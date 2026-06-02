# Authenticode verification under Wine

The PRTG probe verifies the Authenticode signature of every
`MonitoringModules/paessler/*.dll` before loading it (a standard
`WinVerifyTrust(WINTRUST_ACTION_GENERIC_VERIFY_V2)` check, like any
signature-checking Windows application) and confirms the signer is Paessler.

The modules are validly signed (Paessler → SSL.com Code Signing CA → SSL.com
Root, SHA-256, timestamped). **Stock Wine 11.0 mis-handles that verification**, so
the genuine modules are reported as "Signature is not valid" and fail to load.
This is a Wine compatibility gap, not a problem with the modules. The probe binary
ships **unmodified**; the fix lives entirely in Wine's open-source components.

---

## The two Wine gaps

**1. The SOFTPUB trust provider is not registered in a fresh Wine prefix.**
`WinVerifyTrust` then becomes a silent no-op: it returns `0` (success) for
*everything* — signed, unsigned, even a nonexistent file — and produces an empty
signer chain. The probe's signer-identity check has nothing to read, so a valid
module is rejected. Fixed by registering the provider in the prefix at build time
(`wine regsvr32 wintrust.dll`).

**2. Wine hashes PKCS#7 SignedAttributes in the wrong order on the verify path.**
With the provider registered, Wine then returns `TRUST_E_CERT_SIGNATURE`
(`0x80096004`) on the genuine modules. Wine's
`crypt32!CSignedMsgData_UpdateAuthenticatedAttributes` re-encodes the
SignedAttributes via `CryptEncodeObjectEx(PKCS_ATTRIBUTES)`, which **sorts the SET
into DER-canonical order** before hashing. Microsoft's `signtool` emits those
attributes **unsorted** and signs the bytes as they appear; Windows hashes them
as received. Wine's re-sorted hash never matches the signature, so verification
fails for every `signtool`-signed binary. (Binaries signed with `osslsigncode`
happen to verify under stock Wine because OpenSSL emits the attributes already
DER-sorted, matching Wine's re-encoding — which is why the bug is easy to miss.)

The relevant Wine source is `dlls/crypt32/msg.c`:

```c
/* CSignedMsgData_UpdateAuthenticatedAttributes */
ret = CryptEncodeObjectEx(X509_ASN_ENCODING, PKCS_ATTRIBUTES,   /* sorts the SET */
        &msg_data->info->rgSignerInfo[i].AuthAttrs,
        CRYPT_ENCODE_ALLOC_FLAG, NULL, &encodedAttrs, &size);
ret = CryptHashData(msg_data->signerHandles[i].authAttrHash, encodedAttrs, size, 0);
```

This is correct for **signing** but wrong for **verifying**: Windows hashes the
attributes as encoded on the wire. Importing the cert chain does not help — the
failure is at message-signature verification, upstream of all chain/trust
evaluation.

---

## The fix

**(a) Register the SOFTPUB provider — `Dockerfile.prod`.** A build step runs
`wine regsvr32 wintrust.dll` (after the prefix is fully established) and fails the
build if the provider GUID is not registered afterward.

**(b) One-function Wine `crypt32.dll` patch — `docker/wine-patches/`.** On the
**verify** path only, `CSignedMsgData_UpdateAuthenticatedAttributes` hashes the
SignedAttributes in their decoded, on-the-wire order (each attribute encoded
individually, concatenated in array order, wrapped in a SET) — matching what was
signed. The signing path is unchanged. The rebuilt builtin `crypt32.dll` replaces
Wine's at `/opt/wine-stable/lib/wine/i386-windows/`.

- Patch: [`wine-patches/crypt32-msg.c.patch`](wine-patches/crypt32-msg.c.patch)
- Reproducible build: [`wine-patches/build-crypt32.sh`](wine-patches/build-crypt32.sh)
- Idempotent, version-guarded patcher: [`wine-patches/patch-crypt32-msg.py`](wine-patches/patch-crypt32-msg.py)

---

## Verified result

With the patched `crypt32` and the registered provider, verification is **performed
and satisfied** on genuine modules and correctly rejects bad ones:

| Input | `WinVerifyTrust` result |
|-------|-------------------------|
| signed Paessler module | `0x00000000`, signer chain present (cChain=3: leaf → SSL.com Code Signing CA → SSL.com Root) |
| unsigned PE | `0x800B0100` `TRUST_E_NOSIGNATURE` |
| tampered module | `0x80096010` `TRUST_E_BAD_DIGEST` |

End-to-end against the unmodified `PRTG Probe.exe` in service-mode startup: all
**32/32** modules register their sensor catalog, with **0** "Signature is not
valid" and **0** load failures. The security check is restored, not bypassed.

---

## Upstreaming

Gap #2 is a genuine Wine bug: signature verification must hash a signer's
SignedAttributes in their encoded order (as Windows does), not a re-sorted DER
SET. The patch is suitable for a WineHQ bug report and merge request against
`dlls/crypt32/msg.c`. Until it lands upstream, re-run
[`wine-patches/build-crypt32.sh`](wine-patches/build-crypt32.sh) on any Wine
version bump (the patcher is version-guarded and refuses a changed upstream).
