#!/usr/bin/env python3
"""Patch Wine crypt32 msg.c so signature VERIFICATION hashes the SignedAttributes
in their original on-the-wire order (as Windows does) instead of re-sorting them
into DER-canonical SET order. Fixes TRUST_E_CERT_SIGNATURE on signtool-signed
(Authenticode) binaries whose authenticated attributes are not DER-sorted.

Idempotent + version-guarded: asserts the exact upstream text before editing."""
import sys, re

path = sys.argv[1]
src = open(path).read()

MARK = "CRYPT_HashSignedAttrsInOrder"
if MARK in src:
    print("[patch_msg] already patched - no-op")
    sys.exit(0)

# ---- 1. the helper, inserted just before the function that hashes the attrs ----
ANCHOR = "static BOOL CSignedMsgData_UpdateAuthenticatedAttributes(\n CSignedMsgData *msg_data, SignOrVerify flag)\n{"
assert src.count(ANCHOR) == 1, "anchor (UpdateAuthenticatedAttributes signature) not found exactly once"

HELPER = r'''/* Hash a signer's authenticated (signed) attributes for signature verification
 * the way Windows does: over the attributes in their original, on-the-wire
 * order.  Microsoft's signtool emits the SignedAttributes SET WITHOUT sorting it
 * into DER-canonical SET-OF order, and Windows' crypt32 hashes the bytes as they
 * were received.  The normal CryptEncodeObjectEx(PKCS_ATTRIBUTES) path re-encodes
 * and sorts the SET, producing a different hash, so verifying signtool-signed
 * binaries (e.g. Authenticode-signed PE files) failed with TRUST_E_CERT_SIGNATURE.
 * Encode each attribute individually and emit the SET in the decoded order so the
 * hash matches the signed bytes. */
static BOOL CRYPT_HashSignedAttrsInOrder(CSignedMsgData *msg_data, DWORD i)
{
    const CRYPT_ATTRIBUTES *attrs = &msg_data->info->rgSignerInfo[i].AuthAttrs;
    BOOL ret = TRUE;
    DWORD j, bodyLen = 0, lenBytes = 0;
    LPBYTE *items;
    DWORD *sizes;
    LPBYTE out;

    items = CryptMemAlloc(attrs->cAttr * sizeof(*items));
    sizes = CryptMemAlloc(attrs->cAttr * sizeof(*sizes));
    if (!items || !sizes)
    {
        CryptMemFree(items);
        CryptMemFree(sizes);
        return FALSE;
    }
    for (j = 0; j < attrs->cAttr; j++)
        items[j] = NULL;
    for (j = 0; ret && j < attrs->cAttr; j++)
    {
        ret = CryptEncodeObjectEx(X509_ASN_ENCODING, PKCS_ATTRIBUTE,
         &attrs->rgAttr[j], CRYPT_ENCODE_ALLOC_FLAG, NULL, &items[j], &sizes[j]);
        if (ret)
            bodyLen += sizes[j];
    }
    if (ret)
    {
        CRYPT_EncodeLen(bodyLen, NULL, &lenBytes);
        out = LocalAlloc(0, 1 + lenBytes + bodyLen);
        if (out)
        {
            LPBYTE p = out;

            *p++ = 0x31; /* ASN.1 SET OF, constructed */
            CRYPT_EncodeLen(bodyLen, p, &lenBytes);
            p += lenBytes;
            for (j = 0; j < attrs->cAttr; j++)
            {
                memcpy(p, items[j], sizes[j]);
                p += sizes[j];
            }
            ret = CryptHashData(msg_data->signerHandles[i].authAttrHash,
             out, 1 + lenBytes + bodyLen, 0);
            LocalFree(out);
        }
        else
            ret = FALSE;
    }
    for (j = 0; j < attrs->cAttr; j++)
        LocalFree(items[j]);
    CryptMemFree(items);
    CryptMemFree(sizes);
    return ret;
}

'''
src = src.replace(ANCHOR, HELPER + ANCHOR, 1)

# ---- 2. branch the encode/hash block: Verify -> on-wire order ----
OLD = """            if (ret)
            {
                LPBYTE encodedAttrs;
                DWORD size;

                ret = CryptEncodeObjectEx(X509_ASN_ENCODING, PKCS_ATTRIBUTES,
                 &msg_data->info->rgSignerInfo[i].AuthAttrs,
                 CRYPT_ENCODE_ALLOC_FLAG, NULL, &encodedAttrs, &size);
                if (ret)
                {
                    ret = CryptHashData(
                     msg_data->signerHandles[i].authAttrHash, encodedAttrs,
                     size, 0);
                    LocalFree(encodedAttrs);
                }
            }"""
NEW = """            if (ret && flag == Verify)
            {
                /* hash signed attrs in on-the-wire order (Windows-compatible) */
                ret = CRYPT_HashSignedAttrsInOrder(msg_data, i);
            }
            else if (ret)
            {
                LPBYTE encodedAttrs;
                DWORD size;

                ret = CryptEncodeObjectEx(X509_ASN_ENCODING, PKCS_ATTRIBUTES,
                 &msg_data->info->rgSignerInfo[i].AuthAttrs,
                 CRYPT_ENCODE_ALLOC_FLAG, NULL, &encodedAttrs, &size);
                if (ret)
                {
                    ret = CryptHashData(
                     msg_data->signerHandles[i].authAttrHash, encodedAttrs,
                     size, 0);
                    LocalFree(encodedAttrs);
                }
            }"""
assert src.count(OLD) == 1, "encode/hash block not found exactly once (upstream changed?)"
src = src.replace(OLD, NEW, 1)

open(path, "w").write(src)
print("[patch_msg] OK - inserted helper + verify branch")
