#!/usr/bin/env python3
# =============================================================================
#  patch-kernelbase-impersonate.py — idempotent source patch for Wine 11.0's
#  dlls/kernelbase/security.c :: ImpersonateLoggedOnUser().
# =============================================================================
#  WHY: .NET's WindowsIdentity.Impersonate(IntPtr.Zero) is documented to mean
#  "revert to the process/self identity" (no impersonation). The Mono runtime
#  implements this by calling Win32 ImpersonateLoggedOnUser(NULL). Stock Wine
#  fails that call — GetTokenInformation(NULL,...) returns FALSE, so the whole
#  function returns FALSE, and Mono then raises
#      SecurityException: "Couldn't impersonate token."
#
#  Real Windows tolerates the IntPtr.Zero / revert case; Wine does not. This is
#  a genuine Wine gap (same class as the crypt32 SignedAttributes fix): we make
#  the open-source runtime behave per the Win32/.NET contract so the UNMODIFIED
#  vendor helper (e.g. PRTG's LastWinUpdateXML.exe) runs as-is.
#
#  FIX: treat a NULL token as RevertToSelf() (which Wine implements correctly,
#  verified returning TRUE). RevertToSelf is exported by the same DLL.
# =============================================================================
import sys, re

MARKER = "/* prtg-docker: NULL token == revert-to-self"
INSERT = (
    "    if (!token) return RevertToSelf();  "
    + MARKER + " (.NET Impersonate(IntPtr.Zero)) */\n"
)

def main(path):
    src = open(path, encoding="utf-8").read()
    if MARKER in src:
        print("already patched:", path)
        return 0
    # Anchor on the exact function signature, then insert right after its
    # opening brace + declaration block (before the first statement).
    sig = "BOOL WINAPI ImpersonateLoggedOnUser( HANDLE token )\n{\n"
    idx = src.find(sig)
    if idx < 0:
        print("ERROR: ImpersonateLoggedOnUser signature not found — Wine version drift?",
              file=sys.stderr)
        return 2
    # Insert immediately after the declarations (after 'static BOOL warn = TRUE;')
    decl = "    static BOOL warn = TRUE;\n"
    dpos = src.find(decl, idx)
    if dpos < 0 or dpos > idx + 400:
        print("ERROR: declaration anchor not found — Wine version drift?", file=sys.stderr)
        return 2
    at = dpos + len(decl)
    src = src[:at] + "\n" + INSERT + src[at:]
    open(path, "w", encoding="utf-8").write(src)
    print("patched:", path)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
