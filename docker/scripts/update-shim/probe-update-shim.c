/*
 * probe-update-shim.c  —  interception stub that replaces PRTG's PRTGProbeUpdate.exe
 * ----------------------------------------------------------------------------------
 * WHAT PRTGProbeUpdate.exe NORMALLY DOES
 *   When the PRTG core pushes a probe update, "PRTG Probe.exe" downloads the new
 *   installer into  C:\ProgramData\Paessler\PRTG Network Monitor\download\ , copies
 *   PRTGProbeUpdate.exe -> PRTGProbeUpdate_tmp.exe, and launches the copy as:
 *       PRTGProbeUpdate_tmp "<probe exe / mode>" "<installer .exe>"
 *   The real updater then: stops PRTGProbeService, taskkill's "PRTG Probe.exe" and
 *   "PRTG Probe Administrator.exe", runs the Inno Setup installer
 *       <installer> /VERYSILENT /SUPPRESSMSGBOXES /LOG="..." /NORESTART /RESTARTEXITCODE=88
 *   in place, then restarts the service.  Under Wine/Docker that in-place reinstall
 *   would mutate (and likely corrupt) the live Wine prefix and is exactly what we do
 *   NOT want — the canonical, reproducible path is build-probe.py -> a fresh image.
 *
 * WHAT THIS SHIM DOES INSTEAD  (the "installer execution shim", AUTO-UPDATE.md §6)
 *   - Records the full invocation (every argv) to
 *       <data>\Logs\prtg-update-intercept.log
 *   - Best-effort identifies the installer .exe argument and writes a request flag
 *       <data>\update\update-requested.flag        (key=value lines, latest wins)
 *     which the prtg-updater watchdog also reads as an explicit, immediate signal
 *     (it independently watches the download\ dir, so the flag is belt-and-braces).
 *   - Does NOT stop the service, does NOT kill the probe, does NOT run the installer.
 *     The running probe keeps monitoring on the CURRENT version with zero gap; the
 *     watchdog rebuilds the image out-of-band and swaps the container in cleanly.
 *   - Exits 0 so the probe's update state machine sees "updater launched OK" and
 *     does not fall into the "Update application not found, start manually" path or
 *     spin retrying.
 *
 * Build:  see build-shim.sh  (i686-w64-mingw32-gcc, 32-bit, matches the probe).
 * This is intentionally tiny, dependency-free, and reversible (the Dockerfile keeps
 * the original updater as PRTGProbeUpdate.orig.exe).
 */
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static void data_dir(wchar_t *out, size_t n) {
    wchar_t pd[MAX_PATH] = L"C:\\ProgramData";
    GetEnvironmentVariableW(L"ProgramData", pd, MAX_PATH);
    _snwprintf(out, n, L"%ls\\Paessler\\PRTG Network Monitor", pd);
}

static void ensure_dir(const wchar_t *p) { CreateDirectoryW(p, NULL); }

static void now_str(wchar_t *out, size_t n) {
    SYSTEMTIME t; GetLocalTime(&t);
    _snwprintf(out, n, L"%04d-%02d-%02d %02d:%02d:%02d",
               t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond);
}

/* token looks like a downloaded installer (ends .exe, not our own updater/probe exes) */
static int looks_like_installer(const wchar_t *a) {
    size_t L = wcslen(a);
    if (L < 4) return 0;
    if (_wcsicmp(a + L - 4, L".exe") != 0) return 0;
    if (wcsstr(a, L"PRTGProbeUpdate")) return 0;
    if (wcsstr(a, L"PRTG Probe.exe")) return 0;
    if (wcsstr(a, L"Administrator")) return 0;
    return 1;
}

int wmain(int argc, wchar_t **argv) {
    wchar_t data[MAX_PATH], logdir[MAX_PATH], updir[MAX_PATH];
    wchar_t logpath[MAX_PATH], flagpath[MAX_PATH], ts[32];

    data_dir(data, MAX_PATH);
    _snwprintf(logdir, MAX_PATH, L"%ls\\Logs", data);
    _snwprintf(updir,  MAX_PATH, L"%ls\\update", data);
    ensure_dir(data); ensure_dir(logdir); ensure_dir(updir);
    _snwprintf(logpath,  MAX_PATH, L"%ls\\prtg-update-intercept.log", logdir);
    _snwprintf(flagpath, MAX_PATH, L"%ls\\update-requested.flag", updir);
    now_str(ts, 32);

    /* find the installer argument (best effort) */
    const wchar_t *installer = NULL;
    for (int i = 1; i < argc; i++)
        if (looks_like_installer(argv[i])) { installer = argv[i]; break; }

    /* 1) append the full invocation to the intercept log */
    FILE *lf = _wfopen(logpath, L"a, ccs=UTF-8");
    if (lf) {
        fwprintf(lf, L"%ls  PRTGProbeUpdate intercepted (shim) — argc=%d\n", ts, argc);
        for (int i = 0; i < argc; i++)
            fwprintf(lf, L"%ls    argv[%d]=%ls\n", ts, i, argv[i]);
        fwprintf(lf, L"%ls    installer=%ls\n", ts, installer ? installer : L"(not identified)");
        fwprintf(lf, L"%ls    action=recorded; NOT running in-place installer; "
                     L"prtg-updater will rebuild+swap the container.\n", ts);
        fclose(lf);
    }

    /* 2) write/overwrite the request flag the watchdog consumes */
    FILE *ff = _wfopen(flagpath, L"w, ccs=UTF-8");
    if (ff) {
        fwprintf(ff, L"requested_at=%ls\n", ts);
        fwprintf(ff, L"installer=%ls\n", installer ? installer : L"");
        fwprintf(ff, L"argc=%d\n", argc);
        fwprintf(ff, L"cmdline=%ls\n", GetCommandLineW());
        fwprintf(ff, L"handled_by=prtg-updater\n");
        fclose(ff);
    }

    /* 3) succeed — the probe believes the update is underway; keep it running. */
    return 0;
}
