# wine-patches — patched Wine 11.0 builtin DLLs

This directory holds **drop-in replacements for Wine 11.0 builtin DLLs** that fix
gaps where stock Wine diverges from the Windows/.NET contract, so that the
**unmodified Paessler binaries run as-is**. Each is a one-function source patch +
reproducible MinGW rebuild — the same philosophy throughout: fix the open-source
runtime, never the vendor binary.

| Builtin | Fixes | Replaces (retired) | Doc |
|---------|-------|--------------------|-----|
| `crypt32.dll` | Authenticode SignedAttributes hashed in on-wire order | a prior binary-level workaround (removed) | [`../CERT-IMPORT-FIX.md`](../CERT-IMPORT-FIX.md) |
| `kernelbase.dll` | `ImpersonateLoggedOnUser(NULL)` → revert-to-self (success) | a prior binary-level workaround (removed) | [`../DOTNET-ENGINE-C.md`](../DOTNET-ENGINE-C.md) |

---

## `crypt32.dll` — Authenticode verification

## The bug it fixes

The PRTG probe gates every `MonitoringModules/paessler/*.dll` on
`WinVerifyTrust(WINTRUST_ACTION_GENERIC_VERIFY_V2)` and then extracts the signer
certificate to confirm it is Paessler's. The modules are validly Authenticode
signed (Paessler GmbH → SSL.com Code Signing CA → SSL.com Root, SHA-256,
timestamped). Under **stock** Wine 11.0 this verification fails for two reasons:

1. **The SOFTPUB trust provider is not registered** in a fresh prefix, so
   `WinVerifyTrust` is a silent no-op that parses nothing → empty signer chain.
   Fixed at build time by `wine regsvr32 wintrust.dll` (see `Dockerfile.prod`).

2. **`crypt32!CSignedMsgData_UpdateAuthenticatedAttributes` re-encodes the PKCS#7
   SignedAttributes via `CryptEncodeObjectEx(PKCS_ATTRIBUTES)`, which sorts the
   SET into DER-canonical order before hashing.** Microsoft's signtool emits those
   attributes *unsorted* and signs the bytes as they appear; Windows hashes them
   as-received. Wine's re-sorted hash never matches the signature, so verification
   returns `TRUST_E_CERT_SIGNATURE` (0x80096004) for every signtool-signed binary.

   The patch (`crypt32-msg.c.patch`) makes the **verify** path hash the attributes
   in their original on-the-wire order (the signing path is unchanged). Proven:
   the RSA signature verifies over the on-wire attribute order and fails over the
   sorted order — see `CERT-IMPORT-FIX.md` for the byte-level proof.

## Files

| File | What |
|------|------|
| `crypt32.dll` | Prebuilt i386 (win32 PE) Wine 11.0 crypt32 with the fix. Installed into `/opt/wine-stable/lib/wine/i386-windows/crypt32.dll`. |
| `crypt32-msg.c.patch` | Unified diff against Wine 11.0 `dlls/crypt32/msg.c`. |
| `patch-crypt32-msg.py` | Idempotent, version-guarded patcher (asserts exact upstream text). |
| `build-crypt32.sh` | Reproducibly rebuilds `crypt32.dll` in a MinGW Docker builder. |

## Verifying the fix

After `WinVerifyTrust(GENERIC_VERIFY_V2)` against a module:

| Input | Stock Wine | Patched crypt32 + registered provider |
|-------|-----------|---------------------------------------|
| signed Paessler module | `0x0`, **empty signer chain** | `0x0`, **cChain=3** (leaf→intermediate→root) ✓ |
| unsigned PE | `0x0` (no-op) | `TRUST_E_NOSIGNATURE` ✓ |
| tampered module | `0x0` (no-op) | `TRUST_E_BAD_DIGEST` ✓ |

So real verification now succeeds on genuine modules **and** rejects bad ones —
the security check is restored, not bypassed.

## Upstreaming

This is a genuine Wine bug (verification must hash SignedAttributes in encoded
order, matching Windows). The patch is suitable for a WineHQ bug report + MR
against `dlls/crypt32/msg.c`. Until merged upstream, re-run `build-crypt32.sh` on
a Wine version bump.

---

## `kernelbase.dll` — `ImpersonateLoggedOnUser(NULL)`

### The bug it fixes

.NET's `WindowsIdentity.Impersonate(IntPtr.Zero)` is documented to mean "revert to
the process/self identity" (no impersonation) and **succeeds** on Windows. The Mono
runtime (Wine-Mono 6.13.0) implements it by calling Win32
`ImpersonateLoggedOnUser(NULL)`. Stock Wine's `kernelbase!ImpersonateLoggedOnUser`
starts with `GetTokenInformation(token, …)`, which fails for a NULL handle, so the
whole function returns `FALSE` and Mono raises:

```
System.Security.SecurityException: Couldn't impersonate token.
```

Verified on the live probe (relay trace): `Impersonate(<valid OpenProcessToken
handle>)` works; only the `IntPtr.Zero` sentinel was unhandled —
`advapi32.DuplicateToken(NULL)` → `STATUS_INVALID_HANDLE`, then
`advapi32.ImpersonateLoggedOnUser(NULL)` → FALSE.

The patch (`patch-kernelbase-impersonate.py`) adds a single guard at the top of
`ImpersonateLoggedOnUser`: a NULL token returns `RevertToSelf()` (which Wine
implements correctly, verified returning TRUE). This is a strict superset of stock
behavior — no probe code relies on the documented NULL-failure path.

### Files

| File | What |
|------|------|
| `kernelbase.dll` | Prebuilt i386 Wine 11.0 kernelbase with the fix. Installed into `/opt/wine-stable/lib/wine/i386-windows/kernelbase.dll`. |
| `patch-kernelbase-impersonate.py` | Idempotent, version-guarded source patch for `dlls/kernelbase/security.c`. |
| `build-kernelbase.sh` | Reproducibly rebuilds `kernelbase.dll` (fetches Wine source on the host, builds in a MinGW Docker builder). |

### Verifying the fix

```
# stock:   WindowsIdentity.Impersonate(IntPtr.Zero) -> SecurityException
# patched: WindowsIdentity.Impersonate(IntPtr.Zero) -> OK (ctx returned, Undo() ok)
```

### Upstreaming

Arguably a Wine conformance gap (the .NET/Win32 revert semantics for a NULL token).
Suitable for a WineHQ report against `dlls/kernelbase/security.c`. Until merged,
re-run `build-kernelbase.sh` on a Wine version bump.
