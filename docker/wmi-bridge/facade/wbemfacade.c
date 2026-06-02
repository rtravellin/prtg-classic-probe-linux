/*
 * wbemfacade.dll — WbemScripting -> Impacket sidecar facade for the Wine/Docker PRTG probe.
 *
 * Wine's wbemprox is a LOCAL-only WMI provider: the probe's
 *   WbemScripting.SWbemLocator.ConnectServer(host, "root\cimv2", user, pass)
 * fails at IWbemLocator::ConnectServer with WBEM_E_TRANSPORT_FAILURE before any DCOM.
 * (See docker/WMI-ANALYSIS.md.)
 *
 * This DLL re-implements the COM class behind CLSID_WbemLocator
 * ({4590F811-1D3A-11D0-891F-00AA004B2E24}) — the object Wine's own wbemdisp.dll
 * (the WbemScripting automation layer) instantiates via CoCreateInstance. By repointing
 * that CLSID's InprocServer32 at this DLL, the probe's native WbemScripting calls flow:
 *
 *   probe (Delphi, WbemScripting) -> Wine wbemdisp (unchanged automation/IDispatch)
 *      -> [THIS DLL] IWbemLocator::ConnectServer  (captures host/namespace/user/pass)
 *      -> [THIS DLL] IWbemServices::ExecQuery      (POSTs WQL+creds to the sidecar)
 *      -> Impacket sidecar (127.0.0.1:8910 /facade) -> real DCOM/WMI -> Windows target
 *      -> rows marshalled back as IEnumWbemClassObject / IWbemClassObject / VARIANT
 *
 * We implement only the COM vtable layer (IWbemLocator, IWbemServices,
 * IEnumWbemClassObject, IWbemClassObject); Wine's wbemdisp does all the
 * IDispatch / VARIANT / SAFEARRAY automation marshalling for the Delphi consumer,
 * so the native WMI sensor classes work unchanged.
 *
 * Build: i686-w64-mingw32-gcc -shared -O2 -o wbemfacade.dll wbemfacade.c wbemfacade.def \
 *            -lole32 -loleaut32 -lws2_32 -static-libgcc
 */

#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0600
#define WINVER 0x0600
#define CINTERFACE
#define COBJMACROS
#include <windows.h>
#include <objbase.h>
#include <oleauto.h>
#include <wbemcli.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

/* ----- WBEM status codes we return (avoid relying on header macros) ----- */
#ifndef WBEM_S_NO_ERROR
#define WBEM_S_NO_ERROR        ((HRESULT)0x00000000L)
#endif
#ifndef WBEM_S_FALSE
#define WBEM_S_FALSE           ((HRESULT)0x00000001L)
#endif
#ifndef WBEM_S_NO_MORE_DATA
#define WBEM_S_NO_MORE_DATA    ((HRESULT)0x00040005L)
#endif
#ifndef WBEM_E_FAILED
#define WBEM_E_FAILED          ((HRESULT)0x80041001L)
#endif
#ifndef WBEM_E_NOT_FOUND
#define WBEM_E_NOT_FOUND       ((HRESULT)0x80041002L)
#endif
#ifndef WBEM_E_NOT_SUPPORTED
#define WBEM_E_NOT_SUPPORTED   ((HRESULT)0x80041024L)
#endif
#ifndef WBEM_E_INVALID_PARAMETER
#define WBEM_E_INVALID_PARAMETER ((HRESULT)0x80041008L)
#endif

/* ----- self-contained GUIDs (no -luuid dependency) ----- */
static const GUID g_IID_IUnknown =
    {0x00000000,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const GUID g_IID_IClassFactory =
    {0x00000001,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const GUID g_CLSID_WbemLocator =
    {0x4590F811,0x1D3A,0x11D0,{0x89,0x1F,0x00,0xAA,0x00,0x4B,0x2E,0x24}};
static const GUID g_IID_IWbemLocator =
    {0xDC12A687,0x737F,0x11CF,{0x88,0x4D,0x00,0xAA,0x00,0x4B,0x2E,0x24}};
static const GUID g_IID_IWbemServices =
    {0x9556DC99,0x828C,0x11CF,{0xA3,0x7E,0x00,0xAA,0x00,0x32,0x40,0xC7}};
static const GUID g_IID_IEnumWbemClassObject =
    {0x027947E1,0xD731,0x11CE,{0xA3,0x57,0x00,0x00,0x00,0x00,0x00,0x01}};
static const GUID g_IID_IWbemClassObject =
    {0xDC12A681,0x737F,0x11CF,{0x88,0x4D,0x00,0xAA,0x00,0x4B,0x2E,0x24}};
static const GUID g_IID_IClientSecurity =
    {0x0000013D,0x0000,0x0000,{0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};

static LONG g_objs = 0;       /* live COM object count (DllCanUnloadNow) */
static LONG g_locks = 0;      /* IClassFactory::LockServer count */

/* ============================ logging ============================ */
static void fac_log(const char *fmt, ...)
{
    const char *path = getenv("WMI_FACADE_LOG");
    if (!path || !*path) return;
    FILE *f = fopen(path, "a");
    if (!f) return;
    va_list ap; va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

/* ============================ small helpers ============================ */

/* wide (UTF-16) -> freshly malloc'd UTF-8 (caller free()s). NULL -> "" */
static char *w2u(const WCHAR *w)
{
    if (!w) { char *e = (char*)malloc(1); if (e) e[0]=0; return e; }
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 0) { char *e = (char*)malloc(1); if (e) e[0]=0; return e; }
    char *s = (char*)malloc(n);
    if (s) WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL);
    return s;
}

/* UTF-8 (len bytes, not necessarily NUL-terminated) -> BSTR */
static BSTR u2bstr(const char *s, int len)
{
    if (!s) return SysAllocString(L"");
    int n = MultiByteToWideChar(CP_UTF8, 0, s, len, NULL, 0);
    BSTR b = SysAllocStringLen(NULL, n);
    if (b) MultiByteToWideChar(CP_UTF8, 0, s, len, b, n);
    return b;
}

/* append-to-heap-string builder for JSON request */
typedef struct { char *p; size_t len, cap; } sb_t;
static void sb_init(sb_t *b){ b->cap=256; b->len=0; b->p=(char*)malloc(b->cap); if(b->p) b->p[0]=0; }
static void sb_putn(sb_t *b, const char *s, size_t n){
    if(!b->p) return;
    if(b->len+n+1 > b->cap){ while(b->len+n+1>b->cap) b->cap*=2; b->p=(char*)realloc(b->p,b->cap); if(!b->p) return; }
    memcpy(b->p+b->len, s, n); b->len+=n; b->p[b->len]=0;
}
static void sb_puts(sb_t *b, const char *s){ sb_putn(b, s, strlen(s)); }
/* append s as a JSON string literal (with surrounding quotes), escaped */
static void sb_json(sb_t *b, const char *s){
    sb_putn(b, "\"", 1);
    for(; s && *s; ++s){
        unsigned char c = (unsigned char)*s;
        switch(c){
            case '"':  sb_puts(b,"\\\""); break;
            case '\\': sb_puts(b,"\\\\"); break;
            case '\n': sb_puts(b,"\\n");  break;
            case '\r': sb_puts(b,"\\r");  break;
            case '\t': sb_puts(b,"\\t");  break;
            default:
                if(c < 0x20){ char u[8]; sprintf(u,"\\u%04x",c); sb_puts(b,u); }
                else { char ch=(char)c; sb_putn(b,&ch,1); }
        }
    }
    sb_putn(b, "\"", 1);
}

/* ============================ sidecar HTTP (winsock) ============================ */
/* POST `body` to http://<host>:<port>/facade ; return malloc'd response body,
 * set *out_len. Returns NULL on transport error. */
static char *sidecar_post(const char *json, int json_len, int *out_len)
{
    const char *host = getenv("WMI_FACADE_HOST"); if(!host||!*host) host="127.0.0.1";
    const char *ps   = getenv("WMI_FACADE_PORT"); int port = ps&&*ps ? atoi(ps):8910;

    WSADATA wsa; int wsa_started = 0;
    if (WSAStartup(MAKEWORD(2,2), &wsa) == 0) wsa_started = 1;

    char portstr[16]; sprintf(portstr, "%d", port);
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    char *resp = NULL;
    SOCKET s = INVALID_SOCKET;

    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) {
        fac_log("facade: getaddrinfo(%s:%d) failed", host, port);
        goto done;
    }
    s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s == INVALID_SOCKET) { fac_log("facade: socket() failed"); goto done; }
    if (connect(s, res->ai_addr, (int)res->ai_addrlen) != 0) {
        fac_log("facade: connect(%s:%d) failed (sidecar down?)", host, port);
        goto done;
    }

    /* build request */
    sb_t rq; sb_init(&rq);
    {
        char hdr[512];
        sprintf(hdr, "POST /facade HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n", host, json_len);
        sb_puts(&rq, hdr);
        /* shared-secret gate: the sidecar requires this when BRIDGE_TOKEN is set. */
        const char *tok = getenv("WMI_FACADE_TOKEN");
        if (tok && *tok) {
            char th[640];
            sprintf(th, "X-Bridge-Token: %.500s\r\n", tok);
            sb_puts(&rq, th);
        }
        sb_puts(&rq, "\r\n");
    }
    sb_putn(&rq, json, json_len);

    /* send all */
    {
        size_t off = 0;
        while (off < rq.len) {
            int n = send(s, rq.p + off, (int)(rq.len - off), 0);
            if (n <= 0) { fac_log("facade: send failed"); free(rq.p); goto done; }
            off += (size_t)n;
        }
    }
    free(rq.p);

    /* read until close */
    {
        size_t cap = 8192, len = 0;
        char *buf = (char*)malloc(cap);
        if (!buf) goto done;
        for (;;) {
            if (len + 4096 > cap) { cap *= 2; char *nb = (char*)realloc(buf, cap); if(!nb){ free(buf); goto done; } buf = nb; }
            int n = recv(s, buf + len, (int)(cap - len), 0);
            if (n <= 0) break;
            len += (size_t)n;
        }
        /* split headers/body on \r\n\r\n */
        char *body = NULL; size_t blen = 0;
        for (size_t i = 0; i + 3 < len; ++i) {
            if (buf[i]=='\r'&&buf[i+1]=='\n'&&buf[i+2]=='\r'&&buf[i+3]=='\n') {
                body = buf + i + 4; blen = len - (i + 4); break;
            }
        }
        if (body) {
            resp = (char*)malloc(blen + 1);
            if (resp) { memcpy(resp, body, blen); resp[blen] = 0; *out_len = (int)blen; }
        }
        free(buf);
    }

done:
    if (s != INVALID_SOCKET) closesocket(s);
    if (res) freeaddrinfo(res);
    if (wsa_started) WSACleanup();
    return resp;
}

/* ============================ data model ============================ */
typedef struct {
    WCHAR *name;     /* property name (wide, for _wcsicmp) */
    VARIANT val;     /* already-built VARIANT value */
    long cimtype;    /* declared WMI CIMTYPE (preserved even when val is VT_NULL) */
} fac_prop;

/* forward decls of object constructors */
static IWbemClassObject *obj_create(fac_prop *props, int nprops,
                                    const WCHAR *cls, const WCHAR *server, const WCHAR *ns);
static IEnumWbemClassObject *enum_create(IWbemClassObject **rows, int nrows);

/* ---- parse the sidecar wire format into a row array (returns IEnum) ----
 * "OK\n" <nrows>\n  per row: <nprops>\n  per prop: "<namelen> <vt> <vallen>\n"<name><val>
 * On "ERR\n..." or malformed input, *hr is set and NULL returned. */
static IEnumWbemClassObject *parse_wire(const char *buf, int len, HRESULT *hr,
                                        const WCHAR *cls, const WCHAR *server, const WCHAR *ns)
{
    const char *p = buf, *end = buf + len;
    *hr = WBEM_E_FAILED;

    /* status line */
    if (end - p < 3) { fac_log("facade: short response"); return NULL; }
    if (memcmp(p, "OK\n", 3) == 0) { p += 3; }
    else {
        /* ERR\n<msg>\n -> log and fail */
        const char *nl = memchr(p, '\n', end - p);
        const char *m = nl ? nl + 1 : p;
        int mlen = (int)(end - m);
        fac_log("facade: sidecar error: %.*s", mlen > 0 ? mlen : 0, m);
        return NULL;
    }

    /* nrows */
    char *q;
    long nrows = strtol(p, &q, 10);
    if (q == p || nrows < 0) return NULL;
    p = q; if (p < end && *p == '\n') p++;

    IWbemClassObject **rows = NULL;
    if (nrows > 0) {
        rows = (IWbemClassObject**)calloc((size_t)nrows, sizeof(*rows));
        if (!rows) return NULL;
    }

    for (long r = 0; r < nrows; ++r) {
        long nprops = strtol(p, &q, 10);
        if (q == p || nprops < 0) goto fail;
        p = q; if (p < end && *p == '\n') p++;

        fac_prop *props = NULL;
        if (nprops > 0) {
            props = (fac_prop*)calloc((size_t)nprops, sizeof(*props));
            if (!props) goto fail;
        }

        for (long i = 0; i < nprops; ++i) {
            /* "<namelen> <vt> <cimtype> <vallen>\n<name><val>" */
            long namelen = strtol(p, &q, 10); if (q==p) { free(props); goto fail; } p = q;
            while (p < end && *p == ' ') p++;
            char vt = (p < end) ? *p++ : 'S';
            while (p < end && *p == ' ') p++;
            long cimtype = strtol(p, &q, 10); if (q==p) { free(props); goto fail; } p = q;
            while (p < end && *p == ' ') p++;
            long vallen = strtol(p, &q, 10); if (q==p) { free(props); goto fail; } p = q;
            if (p < end && *p == '\n') p++;
            if (namelen < 0 || vallen < 0 || p + namelen + vallen > end) { free(props); goto fail; }

            const char *namep = p; p += namelen;
            const char *valp  = p; p += vallen;

            /* name -> wide */
            BSTR wname = u2bstr(namep, (int)namelen);
            props[i].name = wname ? wname : NULL;  /* reuse as plain wide buffer */
            props[i].cimtype = cimtype;

            VARIANT *v = &props[i].val;
            VariantInit(v);
            switch (vt) {
                case 'I': {
                    char tmp[32]; long n = vallen<31?vallen:31; memcpy(tmp,valp,n); tmp[n]=0;
                    v->vt = VT_I4; v->lVal = (LONG)strtol(tmp, NULL, 10);
                    break; }
                case 'D': {
                    char tmp[64]; long n = vallen<63?vallen:63; memcpy(tmp,valp,n); tmp[n]=0;
                    v->vt = VT_R8; v->dblVal = atof(tmp);
                    break; }
                case 'B': {
                    v->vt = VT_BOOL; v->boolVal = (vallen>=1 && valp[0]=='1') ? VARIANT_TRUE : VARIANT_FALSE;
                    break; }
                case 'N': {
                    v->vt = VT_NULL;
                    break; }
                case 'S':
                default: {
                    v->vt = VT_BSTR; v->bstrVal = u2bstr(valp, (int)vallen);
                    break; }
            }
        }

        rows[r] = obj_create(props, (int)nprops, cls, server, ns);
        /* obj_create takes ownership of props/names/variants */
        if (!rows[r]) goto fail;
    }

    *hr = WBEM_S_NO_ERROR;
    fac_log("facade: parse_wire built %ld row(s)", nrows);
    {
        IEnumWbemClassObject *e = enum_create(rows, (int)nrows);
        if (rows) free(rows);  /* enum_create copied the pointer array */
        if (!e) { *hr = WBEM_E_FAILED; return NULL; }
        return e;
    }

fail:
    if (rows) {
        for (long r = 0; r < nrows; ++r) if (rows[r]) IWbemClassObject_Release(rows[r]);
        free(rows);
    }
    return NULL;
}

/* ============================ IWbemClassObject ============================ */
typedef struct {
    IWbemClassObject base;
    LONG ref;
    fac_prop *props;
    int nprops;
    int enumpos;     /* BeginEnumeration cursor */
    /* context for synthesizing WMI system properties (__GENUS/__CLASS/…) that
     * .NET System.Management's ManagementBaseObject reads on every object */
    BSTR cls;        /* class name (from the WQL "FROM <class>") */
    BSTR server;     /* host */
    BSTR ns;         /* namespace, backslash form e.g. root\cimv2 */
} obj_t;

static fac_prop *obj_find(obj_t *o, const WCHAR *name)
{
    for (int i = 0; i < o->nprops; ++i)
        if (o->props[i].name && _wcsicmp(o->props[i].name, name) == 0)
            return &o->props[i];
    return NULL;
}

static HRESULT STDMETHODCALLTYPE obj_QI(IWbemClassObject *This, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    if (IsEqualGUID(riid, &g_IID_IUnknown) || IsEqualGUID(riid, &g_IID_IWbemClassObject)) {
        *ppv = This; IWbemClassObject_AddRef(This); return S_OK;
    }
    *ppv = NULL; return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE obj_AddRef(IWbemClassObject *This)
{ obj_t *o=(obj_t*)This; return (ULONG)InterlockedIncrement(&o->ref); }
static ULONG STDMETHODCALLTYPE obj_Release(IWbemClassObject *This)
{
    obj_t *o=(obj_t*)This;
    LONG r = InterlockedDecrement(&o->ref);
    if (r == 0) {
        for (int i=0;i<o->nprops;++i){ if(o->props[i].name) SysFreeString(o->props[i].name); VariantClear(&o->props[i].val); }
        if (o->cls) SysFreeString(o->cls);
        if (o->server) SysFreeString(o->server);
        if (o->ns) SysFreeString(o->ns);
        free(o->props); free(o);
        InterlockedDecrement(&g_objs);
    }
    return (ULONG)r;
}
static HRESULT STDMETHODCALLTYPE obj_GetQualifierSet(IWbemClassObject *This, IWbemQualifierSet **pp)
{ (void)This; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }

static HRESULT STDMETHODCALLTYPE obj_Get(IWbemClassObject *This, LPCWSTR wszName, long lFlags,
                                         VARIANT *pVal, CIMTYPE *pType, long *plFlavor)
{
    (void)lFlags;
    obj_t *o=(obj_t*)This;
    if (!pVal) return WBEM_E_INVALID_PARAMETER;
    fac_log("facade: obj_Get %ls", wszName ? wszName : L"(null)");
    /* Wine's wbemdisp propertyset_get_Count asks for the system pseudo-property
     * __PROPERTY_COUNT; answer it from our row's property count. */
    if (wszName && _wcsicmp(wszName, L"__PROPERTY_COUNT") == 0) {
        VariantInit(pVal); pVal->vt = VT_I4; pVal->lVal = o->nprops;
        if (pType) *pType = CIM_SINT32;
        if (plFlavor) *plFlavor = 0;
        return WBEM_S_NO_ERROR;
    }
    /* WMI system properties. .NET System.Management's ManagementBaseObject reads
     * __GENUS (to tell class vs instance), __CLASS/__SERVER/__NAMESPACE/__PATH, etc.
     * on every object. Wine's wbemprox synthesizes these; we must too, or the .NET
     * helpers get WBEM_E_NOT_FOUND right after a successful query. Unknown "__*"
     * props return VT_NULL+S_OK (never NOT_FOUND) so Mono's path can't choke. */
    if (wszName && wszName[0] == L'_' && wszName[1] == L'_') {
        VariantInit(pVal);
        if (plFlavor) *plFlavor = 0x20 /*WBEM_FLAVOR_ORIGIN_SYSTEM*/;
        if (_wcsicmp(wszName, L"__GENUS") == 0) {
            pVal->vt = VT_I4; pVal->lVal = 2 /*WBEM_GENUS_INSTANCE*/;
            if (pType) *pType = CIM_SINT32; return WBEM_S_NO_ERROR;
        }
        if (_wcsicmp(wszName, L"__CLASS") == 0 || _wcsicmp(wszName, L"__DYNASTY") == 0 ||
            _wcsicmp(wszName, L"__RELPATH") == 0) {
            pVal->vt = VT_BSTR; pVal->bstrVal = SysAllocString(o->cls ? o->cls : L"");
            if (pType) *pType = CIM_STRING; return WBEM_S_NO_ERROR;
        }
        if (_wcsicmp(wszName, L"__SERVER") == 0) {
            pVal->vt = VT_BSTR; pVal->bstrVal = SysAllocString(o->server ? o->server : L"");
            if (pType) *pType = CIM_STRING; return WBEM_S_NO_ERROR;
        }
        if (_wcsicmp(wszName, L"__NAMESPACE") == 0) {
            pVal->vt = VT_BSTR; pVal->bstrVal = SysAllocString(o->ns ? o->ns : L"root\\cimv2");
            if (pType) *pType = CIM_STRING; return WBEM_S_NO_ERROR;
        }
        if (_wcsicmp(wszName, L"__PATH") == 0 || _wcsicmp(wszName, L"__NAMESPACE_PATH") == 0) {
            WCHAR buf[512];
            _snwprintf(buf, 511, L"\\\\%s\\%s:%s",
                       o->server ? o->server : L".", o->ns ? o->ns : L"root\\cimv2",
                       o->cls ? o->cls : L"");
            buf[511]=0;
            pVal->vt = VT_BSTR; pVal->bstrVal = SysAllocString(buf);
            if (pType) *pType = CIM_STRING; return WBEM_S_NO_ERROR;
        }
        if (_wcsicmp(wszName, L"__DERIVATION") == 0) {
            SAFEARRAY *sa = SafeArrayCreateVector(VT_BSTR, 0, 0);
            pVal->vt = VT_ARRAY|VT_BSTR; pVal->parray = sa;
            if (pType) *pType = CIM_STRING|CIM_FLAG_ARRAY; return WBEM_S_NO_ERROR;
        }
        /* __SUPERCLASS and any other system prop: NULL but present */
        pVal->vt = VT_NULL;
        if (pType) *pType = CIM_STRING;
        return WBEM_S_NO_ERROR;
    }
    fac_prop *pr = obj_find(o, wszName);
    if (!pr) {
        VariantInit(pVal); pVal->vt = VT_NULL;
        if (pType) *pType = CIM_EMPTY;
        if (plFlavor) *plFlavor = 0;
        return WBEM_E_NOT_FOUND;
    }
    VariantInit(pVal);
    VariantCopy(pVal, &pr->val);
    /* report the DECLARED CIMTYPE from WMI (preserved even when the value is VT_NULL),
     * not one derived from the VARIANT — the probe rejects CIM type 0/EMPTY. */
    if (pType) *pType = pr->cimtype ? (CIMTYPE)pr->cimtype : CIM_STRING;
    if (plFlavor) *plFlavor = 0;
    return WBEM_S_NO_ERROR;
}
static HRESULT STDMETHODCALLTYPE obj_Put(IWbemClassObject *This, LPCWSTR n, long f, VARIANT *v, CIMTYPE t)
{ (void)This;(void)n;(void)f;(void)v;(void)t; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_Delete(IWbemClassObject *This, LPCWSTR n)
{ (void)This;(void)n; return WBEM_E_NOT_SUPPORTED; }

static HRESULT STDMETHODCALLTYPE obj_GetNames(IWbemClassObject *This, LPCWSTR wszQ, long lFlags,
                                              VARIANT *pQv, SAFEARRAY **pNames)
{
    (void)wszQ;(void)lFlags;(void)pQv;
    obj_t *o=(obj_t*)This;
    if (!pNames) return WBEM_E_INVALID_PARAMETER;
    fac_log("facade: obj_GetNames nprops=%d lFlags=0x%lx", o->nprops, (unsigned long)lFlags);
    SAFEARRAY *sa = SafeArrayCreateVector(VT_BSTR, 0, (ULONG)o->nprops);
    if (!sa) return WBEM_E_FAILED;
    for (LONG i=0;i<o->nprops;++i){
        BSTR b = SysAllocString(o->props[i].name ? o->props[i].name : L"");
        SafeArrayPutElement(sa, &i, b);
        SysFreeString(b);
    }
    *pNames = sa;
    return WBEM_S_NO_ERROR;
}
static HRESULT STDMETHODCALLTYPE obj_BeginEnumeration(IWbemClassObject *This, long lEnumFlags)
{ (void)lEnumFlags; ((obj_t*)This)->enumpos = 0; fac_log("facade: obj_BeginEnumeration"); return WBEM_S_NO_ERROR; }
static HRESULT STDMETHODCALLTYPE obj_NextProp(IWbemClassObject *This, long lFlags, BSTR *strName,
                                              VARIANT *pVal, CIMTYPE *pType, long *plFlavor)
{
    (void)lFlags;
    obj_t *o=(obj_t*)This;
    if (o->enumpos >= o->nprops) {
        if (strName) *strName = NULL;
        if (pVal) { VariantInit(pVal); }
        if (pType) *pType = CIM_EMPTY;
        if (plFlavor) *plFlavor = 0;
        return WBEM_S_NO_MORE_DATA;
    }
    fac_prop *pr = &o->props[o->enumpos++];
    if (strName) *strName = SysAllocString(pr->name ? pr->name : L"");
    if (pVal) { VariantInit(pVal); VariantCopy(pVal, &pr->val); }
    if (pType) *pType = pr->cimtype ? (CIMTYPE)pr->cimtype : CIM_STRING;
    if (plFlavor) *plFlavor = 0;
    return WBEM_S_NO_ERROR;
}
static HRESULT STDMETHODCALLTYPE obj_EndEnumeration(IWbemClassObject *This)
{ ((obj_t*)This)->enumpos = 0; return WBEM_S_NO_ERROR; }
static HRESULT STDMETHODCALLTYPE obj_GetPropertyQualifierSet(IWbemClassObject *This, LPCWSTR p, IWbemQualifierSet **pp)
{ (void)This;(void)p; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_Clone(IWbemClassObject *This, IWbemClassObject **pp)
{ (void)This; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_GetObjectText(IWbemClassObject *This, long f, BSTR *p)
{ (void)This;(void)f; if(p)*p=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_SpawnDerivedClass(IWbemClassObject *This, long f, IWbemClassObject **pp)
{ (void)This;(void)f; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_SpawnInstance(IWbemClassObject *This, long f, IWbemClassObject **pp)
{ (void)This;(void)f; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_CompareTo(IWbemClassObject *This, long f, IWbemClassObject *p)
{ (void)This;(void)f;(void)p; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_GetPropertyOrigin(IWbemClassObject *This, LPCWSTR n, BSTR *p)
{ (void)This;(void)n; if(p)*p=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_InheritsFrom(IWbemClassObject *This, LPCWSTR n)
{ (void)This;(void)n; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_GetMethod(IWbemClassObject *This, LPCWSTR n, long f, IWbemClassObject **a, IWbemClassObject **b)
{ (void)This;(void)n;(void)f; if(a)*a=NULL; if(b)*b=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_PutMethod(IWbemClassObject *This, LPCWSTR n, long f, IWbemClassObject *a, IWbemClassObject *b)
{ (void)This;(void)n;(void)f;(void)a;(void)b; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_DeleteMethod(IWbemClassObject *This, LPCWSTR n)
{ (void)This;(void)n; return WBEM_E_NOT_SUPPORTED; }
/* Wine's wbemdisp init_members() (used to resolve late-bound property DISPIDs) walks the
 * method list too; return success with an immediately-empty enumeration so it doesn't abort. */
static HRESULT STDMETHODCALLTYPE obj_BeginMethodEnumeration(IWbemClassObject *This, long f)
{ (void)This;(void)f; return WBEM_S_NO_ERROR; }
static HRESULT STDMETHODCALLTYPE obj_NextMethod(IWbemClassObject *This, long f, BSTR *n, IWbemClassObject **a, IWbemClassObject **b)
{ (void)This;(void)f; if(n)*n=NULL; if(a)*a=NULL; if(b)*b=NULL; return WBEM_S_NO_MORE_DATA; }
static HRESULT STDMETHODCALLTYPE obj_EndMethodEnumeration(IWbemClassObject *This)
{ (void)This; return WBEM_S_NO_ERROR; }
static HRESULT STDMETHODCALLTYPE obj_GetMethodQualifierSet(IWbemClassObject *This, LPCWSTR n, IWbemQualifierSet **pp)
{ (void)This;(void)n; if(pp)*pp=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE obj_GetMethodOrigin(IWbemClassObject *This, LPCWSTR n, BSTR *p)
{ (void)This;(void)n; if(p)*p=NULL; return WBEM_E_NOT_SUPPORTED; }

static const IWbemClassObjectVtbl obj_vtbl = {
    obj_QI, obj_AddRef, obj_Release,
    obj_GetQualifierSet, obj_Get, obj_Put, obj_Delete, obj_GetNames,
    obj_BeginEnumeration, obj_NextProp, obj_EndEnumeration,
    obj_GetPropertyQualifierSet, obj_Clone, obj_GetObjectText,
    obj_SpawnDerivedClass, obj_SpawnInstance, obj_CompareTo,
    obj_GetPropertyOrigin, obj_InheritsFrom,
    obj_GetMethod, obj_PutMethod, obj_DeleteMethod,
    obj_BeginMethodEnumeration, obj_NextMethod, obj_EndMethodEnumeration,
    obj_GetMethodQualifierSet, obj_GetMethodOrigin
};

static IWbemClassObject *obj_create(fac_prop *props, int nprops,
                                    const WCHAR *cls, const WCHAR *server, const WCHAR *ns)
{
    obj_t *o = (obj_t*)calloc(1, sizeof(*o));
    if (!o) return NULL;
    o->base.lpVtbl = (IWbemClassObjectVtbl*)&obj_vtbl;
    o->ref = 1;
    o->props = props;
    o->nprops = nprops;
    o->enumpos = 0;
    o->cls    = cls    ? SysAllocString(cls)    : NULL;
    o->server = server ? SysAllocString(server) : NULL;
    o->ns     = ns     ? SysAllocString(ns)     : NULL;
    InterlockedIncrement(&g_objs);
    return (IWbemClassObject*)o;
}

/* ============================ IEnumWbemClassObject ============================ */
typedef struct {
    IEnumWbemClassObject base;
    LONG ref;
    IWbemClassObject **rows;
    int nrows;
    int pos;
} enum_t;

static HRESULT STDMETHODCALLTYPE en_QI(IEnumWbemClassObject *This, REFIID riid, void **ppv)
{
    if (!ppv) return E_POINTER;
    if (IsEqualGUID(riid,&g_IID_IUnknown)||IsEqualGUID(riid,&g_IID_IEnumWbemClassObject)){
        *ppv=This; IEnumWbemClassObject_AddRef(This); return S_OK;
    }
    *ppv=NULL; return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE en_AddRef(IEnumWbemClassObject *This)
{ enum_t *e=(enum_t*)This; return (ULONG)InterlockedIncrement(&e->ref); }
static ULONG STDMETHODCALLTYPE en_Release(IEnumWbemClassObject *This)
{
    enum_t *e=(enum_t*)This;
    LONG r=InterlockedDecrement(&e->ref);
    if(r==0){
        for(int i=0;i<e->nrows;++i) if(e->rows[i]) IWbemClassObject_Release(e->rows[i]);
        free(e->rows); free(e);
        InterlockedDecrement(&g_objs);
    }
    return (ULONG)r;
}
static HRESULT STDMETHODCALLTYPE en_Reset(IEnumWbemClassObject *This)
{ ((enum_t*)This)->pos=0; return WBEM_S_NO_ERROR; }
static HRESULT STDMETHODCALLTYPE en_Next(IEnumWbemClassObject *This, long lTimeout, ULONG uCount,
                                         IWbemClassObject **apObjects, ULONG *puReturned)
{
    (void)lTimeout;
    enum_t *e=(enum_t*)This;
    ULONG got=0;
    if (!apObjects) return WBEM_E_INVALID_PARAMETER;
    fac_log("facade: en_Next uCount=%lu pos=%d nrows=%d", (unsigned long)uCount, e->pos, e->nrows);
    while (got < uCount && e->pos < e->nrows) {
        IWbemClassObject *o = e->rows[e->pos++];
        IWbemClassObject_AddRef(o);
        apObjects[got++] = o;
    }
    if (puReturned) *puReturned = got;
    return (got == uCount) ? WBEM_S_NO_ERROR : WBEM_S_FALSE;
}
static HRESULT STDMETHODCALLTYPE en_NextAsync(IEnumWbemClassObject *This, ULONG u, IWbemObjectSink *s)
{ (void)This;(void)u;(void)s; return WBEM_E_NOT_SUPPORTED; }
/* Wine's wbemdisp objectset_get__NewEnum calls Clone to get an independent iterator for
 * the IEnumVARIANT that drives `For Each`. The clone shares the (immutable) row objects
 * and preserves the current cursor position, per the IEnum* contract. */
static HRESULT STDMETHODCALLTYPE en_Clone(IEnumWbemClassObject *This, IEnumWbemClassObject **pp)
{
    enum_t *e=(enum_t*)This;
    if(!pp) return WBEM_E_INVALID_PARAMETER;
    *pp=NULL;
    enum_t *c=(enum_t*)calloc(1,sizeof(*c));
    if(!c) return E_OUTOFMEMORY;
    c->base.lpVtbl=e->base.lpVtbl;
    c->ref=1;
    c->nrows=e->nrows;
    c->pos=e->pos;
    if(e->nrows>0){
        c->rows=(IWbemClassObject**)calloc((size_t)e->nrows,sizeof(*c->rows));
        if(!c->rows){ free(c); return E_OUTOFMEMORY; }
        for(int i=0;i<e->nrows;++i){ c->rows[i]=e->rows[i]; if(c->rows[i]) IWbemClassObject_AddRef(c->rows[i]); }
    }
    InterlockedIncrement(&g_objs);
    *pp=(IEnumWbemClassObject*)c;
    fac_log("facade: en_Clone -> %p pos=%d nrows=%d", (void*)c, c->pos, c->nrows);
    return WBEM_S_NO_ERROR;
}
static HRESULT STDMETHODCALLTYPE en_Skip(IEnumWbemClassObject *This, long lTimeout, ULONG nCount)
{
    (void)lTimeout;
    enum_t *e=(enum_t*)This;
    ULONG n=nCount;
    while(n && e->pos<e->nrows){ e->pos++; n--; }
    return n? WBEM_S_FALSE : WBEM_S_NO_ERROR;
}
static const IEnumWbemClassObjectVtbl enum_vtbl = {
    en_QI, en_AddRef, en_Release, en_Reset, en_Next, en_NextAsync, en_Clone, en_Skip
};
static IEnumWbemClassObject *enum_create(IWbemClassObject **rows, int nrows)
{
    enum_t *e=(enum_t*)calloc(1,sizeof(*e));
    if(!e) return NULL;
    e->base.lpVtbl=(IEnumWbemClassObjectVtbl*)&enum_vtbl;
    e->ref=1;
    e->nrows=nrows;
    e->pos=0;
    if(nrows>0){
        e->rows=(IWbemClassObject**)calloc((size_t)nrows,sizeof(*rows));
        if(!e->rows){ free(e); return NULL; }
        for(int i=0;i<nrows;++i){ e->rows[i]=rows[i]; }  /* take ownership (no AddRef) */
    }
    InterlockedIncrement(&g_objs);
    return (IEnumWbemClassObject*)e;
}

/* ============================ IWbemServices ============================ */
typedef struct {
    IWbemServices base;
    LONG ref;
    char host[256];
    char ns[256];
    char user[256];
    char pass[512];
    char domain[256];
} svc_t;

static HRESULT STDMETHODCALLTYPE svc_QI(IWbemServices *This, REFIID riid, void **ppv)
{
    if(!ppv) return E_POINTER;
    if(IsEqualGUID(riid,&g_IID_IUnknown)||IsEqualGUID(riid,&g_IID_IWbemServices)){
        *ppv=This; IWbemServices_AddRef(This); return S_OK;
    }
    /* wbemdisp may QI for IClientSecurity to set proxy blanket; we are in-proc,
     * so decline and let it proceed without a blanket. */
    *ppv=NULL; return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE svc_AddRef(IWbemServices *This)
{ svc_t *s=(svc_t*)This; return (ULONG)InterlockedIncrement(&s->ref); }
static ULONG STDMETHODCALLTYPE svc_Release(IWbemServices *This)
{
    svc_t *s=(svc_t*)This;
    LONG r=InterlockedDecrement(&s->ref);
    if(r==0){ free(s); InterlockedDecrement(&g_objs); }
    return (ULONG)r;
}
static HRESULT STDMETHODCALLTYPE svc_OpenNamespace(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemServices **d, IWbemCallResult **e)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; if(e)*e=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_CancelAsyncCall(IWbemServices *This, IWbemObjectSink *a)
{ (void)This;(void)a; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_QueryObjectSink(IWbemServices *This, long a, IWbemObjectSink **b)
{ (void)This;(void)a; if(b)*b=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_GetObject(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemClassObject **d, IWbemCallResult **e)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; if(e)*e=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_GetObjectAsync(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_PutClass(IWbemServices *This, IWbemClassObject *a, long b, IWbemContext *c, IWbemCallResult **d)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_PutClassAsync(IWbemServices *This, IWbemClassObject *a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_DeleteClass(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemCallResult **d)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_DeleteClassAsync(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_CreateClassEnum(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IEnumWbemClassObject **d)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_CreateClassEnumAsync(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_PutInstance(IWbemServices *This, IWbemClassObject *a, long b, IWbemContext *c, IWbemCallResult **d)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_PutInstanceAsync(IWbemServices *This, IWbemClassObject *a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_DeleteInstance(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemCallResult **d)
{ (void)This;(void)a;(void)b;(void)c; if(d)*d=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_DeleteInstanceAsync(IWbemServices *This, const BSTR a, long b, IWbemContext *c, IWbemObjectSink *d)
{ (void)This;(void)a;(void)b;(void)c;(void)d; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_CreateInstanceEnum(IWbemServices *This, const BSTR strClass, long lFlags, IWbemContext *c, IEnumWbemClassObject **ppEnum);
/* The *Async (sink-based / semisynchronous) forms are the path .NET's
 * System.Management uses by default (ManagementObjectSearcher.Get() with
 * EnumerationOptions.ReturnImmediately=true). Real bodies follow svc_do_query. */
static HRESULT STDMETHODCALLTYPE svc_CreateInstanceEnumAsync(IWbemServices *This, const BSTR strClass, long lFlags, IWbemContext *c, IWbemObjectSink *pSink);
static HRESULT STDMETHODCALLTYPE svc_ExecQuery(IWbemServices *This, const BSTR strQueryLanguage,
        const BSTR strQuery, long lFlags, IWbemContext *pCtx, IEnumWbemClassObject **ppEnum);
static HRESULT STDMETHODCALLTYPE svc_ExecQueryAsync(IWbemServices *This, const BSTR strQueryLanguage, const BSTR strQuery, long lFlags, IWbemContext *pCtx, IWbemObjectSink *pSink);
static HRESULT STDMETHODCALLTYPE svc_ExecNotificationQuery(IWbemServices *This, const BSTR a, const BSTR b, long c, IWbemContext *d, IEnumWbemClassObject **e)
{ (void)This;(void)a;(void)b;(void)c;(void)d; if(e)*e=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_ExecNotificationQueryAsync(IWbemServices *This, const BSTR a, const BSTR b, long c, IWbemContext *d, IWbemObjectSink *e)
{ (void)This;(void)a;(void)b;(void)c;(void)d;(void)e; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_ExecMethod(IWbemServices *This, const BSTR a, const BSTR b, long c, IWbemContext *d, IWbemClassObject *e, IWbemClassObject **f, IWbemCallResult **g)
{ (void)This;(void)a;(void)b;(void)c;(void)d;(void)e; if(f)*f=NULL; if(g)*g=NULL; return WBEM_E_NOT_SUPPORTED; }
static HRESULT STDMETHODCALLTYPE svc_ExecMethodAsync(IWbemServices *This, const BSTR a, const BSTR b, long c, IWbemContext *d, IWbemClassObject *e, IWbemObjectSink *f)
{ (void)This;(void)a;(void)b;(void)c;(void)d;(void)e;(void)f; return WBEM_E_NOT_SUPPORTED; }

static const IWbemServicesVtbl svc_vtbl = {
    svc_QI, svc_AddRef, svc_Release,
    svc_OpenNamespace, svc_CancelAsyncCall, svc_QueryObjectSink,
    svc_GetObject, svc_GetObjectAsync,
    svc_PutClass, svc_PutClassAsync, svc_DeleteClass, svc_DeleteClassAsync,
    svc_CreateClassEnum, svc_CreateClassEnumAsync,
    svc_PutInstance, svc_PutInstanceAsync, svc_DeleteInstance, svc_DeleteInstanceAsync,
    svc_CreateInstanceEnum, svc_CreateInstanceEnumAsync,
    svc_ExecQuery, svc_ExecQueryAsync,
    svc_ExecNotificationQuery, svc_ExecNotificationQueryAsync,
    svc_ExecMethod, svc_ExecMethodAsync
};

/* shared query path used by ExecQuery and (synthesized) CreateInstanceEnum */
static HRESULT svc_do_query(svc_t *s, const char *wql, IEnumWbemClassObject **ppEnum)
{
    if (!ppEnum) return WBEM_E_INVALID_PARAMETER;
    *ppEnum = NULL;

    sb_t rq; sb_init(&rq);
    sb_puts(&rq, "{\"host\":");      sb_json(&rq, s->host);
    sb_puts(&rq, ",\"namespace\":"); sb_json(&rq, s->ns);
    sb_puts(&rq, ",\"user\":");      sb_json(&rq, s->user);
    sb_puts(&rq, ",\"password\":");  sb_json(&rq, s->pass);
    sb_puts(&rq, ",\"domain\":");    sb_json(&rq, s->domain);
    sb_puts(&rq, ",\"wql\":");       sb_json(&rq, wql);
    sb_puts(&rq, "}");

    fac_log("facade: ExecQuery host=%s ns=%s user=%s wql=%s", s->host, s->ns, s->user, wql);

    int rlen = 0;
    char *resp = sidecar_post(rq.p, (int)rq.len, &rlen);
    free(rq.p);
    if (!resp) { fac_log("facade: no response from sidecar"); return WBEM_E_FAILED; }
    fac_log("facade: sidecar resp len=%d head=[%.16s]", rlen, resp);

    /* Context for the objects' synthesized system properties: the queried class
     * (parsed from "... FROM <class> ..."), the server (host), and the namespace
     * in WMI backslash form. */
    char clsbuf[128]; clsbuf[0]=0;
    {
        const char *f = wql;
        for (; *f; ++f) {
            if ((f[0]=='f'||f[0]=='F') && (f[1]=='r'||f[1]=='R') && (f[2]=='o'||f[2]=='O') &&
                (f[3]=='m'||f[3]=='M') && (f[4]==' '||f[4]=='\t')) { f += 5; break; }
        }
        while (*f==' '||*f=='\t') f++;
        int n=0; while (*f && *f!=' ' && *f!='\t' && *f!=';' && n<127) clsbuf[n++]=*f++;
        clsbuf[n]=0;
    }
    char nsbuf[128]; { int i=0; for (; s->ns[i] && i<127; ++i) nsbuf[i] = (s->ns[i]=='/')?'\\':s->ns[i]; nsbuf[i]=0; }
    BSTR wcls    = clsbuf[0] ? u2bstr(clsbuf, (int)strlen(clsbuf)) : NULL;
    BSTR wserver = u2bstr(s->host, (int)strlen(s->host));
    BSTR wns     = u2bstr(nsbuf, (int)strlen(nsbuf));

    HRESULT hr = WBEM_E_FAILED;
    IEnumWbemClassObject *e = parse_wire(resp, rlen, &hr, wcls, wserver, wns);
    free(resp);
    if (wcls) SysFreeString(wcls);
    if (wserver) SysFreeString(wserver);
    if (wns) SysFreeString(wns);
    fac_log("facade: parse_wire hr=0x%08lx enum=%p", (unsigned long)hr, (void*)e);
    if (FAILED(hr) || !e) return FAILED(hr) ? hr : WBEM_E_FAILED;
    *ppEnum = e;
    return WBEM_S_NO_ERROR;
}

static HRESULT STDMETHODCALLTYPE svc_ExecQuery(IWbemServices *This, const BSTR strQueryLanguage,
        const BSTR strQuery, long lFlags, IWbemContext *pCtx, IEnumWbemClassObject **ppEnum)
{
    (void)strQueryLanguage;(void)lFlags;(void)pCtx;
    svc_t *s=(svc_t*)This;
    char *wql = w2u(strQuery);
    HRESULT hr = svc_do_query(s, wql ? wql : "", ppEnum);
    free(wql);
    return hr;
}

/* Some WMI code paths use CreateInstanceEnum("Win32_Foo") instead of ExecQuery.
 * Synthesize an equivalent "SELECT * FROM <class>" and reuse the same path. */
static HRESULT STDMETHODCALLTYPE svc_CreateInstanceEnum(IWbemServices *This, const BSTR strClass,
        long lFlags, IWbemContext *c, IEnumWbemClassObject **ppEnum)
{
    (void)lFlags;(void)c;
    svc_t *s=(svc_t*)This;
    char *cls = w2u(strClass);
    sb_t q; sb_init(&q);
    sb_puts(&q, "SELECT * FROM ");
    sb_puts(&q, cls && *cls ? cls : "meta_class");
    HRESULT hr = svc_do_query(s, q.p, ppEnum);
    free(q.p); free(cls);
    return hr;
}

/* WBEM_STATUS_COMPLETE — the sink flag that signals end-of-results. */
#ifndef WBEM_STATUS_COMPLETE
#define WBEM_STATUS_COMPLETE ((long)0)
#endif

/* Sink-based (semisynchronous) enumeration. .NET's System.Management defaults to
 * ReturnImmediately=true, so ManagementObjectSearcher.Get() calls the *Async forms
 * with an IWbemObjectSink rather than the synchronous enum. Real WMI delivers on a
 * worker thread; we run the same sidecar query and deliver every row to the sink via
 * Indicate(), then signal WBEM_STATUS_COMPLETE — delivering synchronously inside the
 * call is permitted and is exactly what Mono's WmiEventSink consumes. */
static HRESULT svc_do_query_async(svc_t *s, const char *wql, IWbemObjectSink *sink)
{
    if (!sink) return WBEM_E_INVALID_PARAMETER;
    IWbemObjectSink_AddRef(sink);
    IEnumWbemClassObject *e = NULL;
    HRESULT hr = svc_do_query(s, wql, &e);
    if (FAILED(hr) || !e) {
        HRESULT st = FAILED(hr) ? hr : WBEM_E_FAILED;
        IWbemObjectSink_SetStatus(sink, WBEM_STATUS_COMPLETE, st, NULL, NULL);
        IWbemObjectSink_Release(sink);
        return hr;
    }
    for (;;) {
        IWbemClassObject *obj = NULL; ULONG ret = 0;
        IEnumWbemClassObject_Next(e, -1 /*WBEM_INFINITE*/, 1, &obj, &ret);
        if (ret == 0 || !obj) break;
        IWbemObjectSink_Indicate(sink, 1, &obj);
        IWbemClassObject_Release(obj);
    }
    IEnumWbemClassObject_Release(e);
    IWbemObjectSink_SetStatus(sink, WBEM_STATUS_COMPLETE, WBEM_S_NO_ERROR, NULL, NULL);
    IWbemObjectSink_Release(sink);
    return WBEM_S_NO_ERROR;
}

static HRESULT STDMETHODCALLTYPE svc_ExecQueryAsync(IWbemServices *This, const BSTR strQueryLanguage,
        const BSTR strQuery, long lFlags, IWbemContext *pCtx, IWbemObjectSink *pSink)
{
    (void)strQueryLanguage;(void)lFlags;(void)pCtx;
    svc_t *s=(svc_t*)This;
    char *wql = w2u(strQuery);
    HRESULT hr = svc_do_query_async(s, wql ? wql : "", pSink);
    free(wql);
    return hr;
}

static HRESULT STDMETHODCALLTYPE svc_CreateInstanceEnumAsync(IWbemServices *This, const BSTR strClass,
        long lFlags, IWbemContext *c, IWbemObjectSink *pSink)
{
    (void)lFlags;(void)c;
    svc_t *s=(svc_t*)This;
    char *cls = w2u(strClass);
    sb_t q; sb_init(&q);
    sb_puts(&q, "SELECT * FROM ");
    sb_puts(&q, cls && *cls ? cls : "meta_class");
    HRESULT hr = svc_do_query_async(s, q.p, pSink);
    free(q.p); free(cls);
    return hr;
}

static IWbemServices *svc_create(void)
{
    svc_t *s=(svc_t*)calloc(1,sizeof(*s));
    if(!s) return NULL;
    s->base.lpVtbl=(IWbemServicesVtbl*)&svc_vtbl;
    s->ref=1;
    InterlockedIncrement(&g_objs);
    return (IWbemServices*)s;
}

/* ============================ IWbemLocator ============================ */
typedef struct { IWbemLocator base; LONG ref; } loc_t;

static HRESULT STDMETHODCALLTYPE loc_QI(IWbemLocator *This, REFIID riid, void **ppv)
{
    if(!ppv) return E_POINTER;
    if(IsEqualGUID(riid,&g_IID_IUnknown)||IsEqualGUID(riid,&g_IID_IWbemLocator)){
        *ppv=This; IWbemLocator_AddRef(This); return S_OK;
    }
    *ppv=NULL; return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE loc_AddRef(IWbemLocator *This)
{ loc_t *l=(loc_t*)This; return (ULONG)InterlockedIncrement(&l->ref); }
static ULONG STDMETHODCALLTYPE loc_Release(IWbemLocator *This)
{
    loc_t *l=(loc_t*)This;
    LONG r=InterlockedDecrement(&l->ref);
    if(r==0){ free(l); InterlockedDecrement(&g_objs); }
    return (ULONG)r;
}

/* parse "\\host\root\cimv2" (or "host\root\cimv2") into host + namespace (slash form) */
static void parse_resource(const char *res, char *host, size_t hs, char *ns, size_t nss)
{
    host[0]=0; ns[0]=0;
    const char *p = res ? res : "";
    while (*p=='\\' || *p=='/') p++;            /* skip leading slashes */
    const char *hstart = p;
    while (*p && *p!='\\' && *p!='/') p++;       /* host component */
    size_t hl = (size_t)(p - hstart); if (hl >= hs) hl = hs-1;
    memcpy(host, hstart, hl); host[hl]=0;
    while (*p=='\\' || *p=='/') p++;
    /* remainder is the namespace */
    size_t i=0;
    for (; *p && i+1<nss; ++p) ns[i++] = (*p=='\\') ? '/' : *p;
    ns[i]=0;
    if (!ns[0]) { strncpy(ns, "root/cimv2", nss-1); ns[nss-1]=0; }
}

static HRESULT STDMETHODCALLTYPE loc_ConnectServer(IWbemLocator *This,
        const BSTR strNetworkResource, const BSTR strUser, const BSTR strPassword,
        const BSTR strLocale, long lSecurityFlags, const BSTR strAuthority,
        IWbemContext *pCtx, IWbemServices **ppNamespace)
{
    (void)This;(void)strLocale;(void)lSecurityFlags;(void)pCtx;
    if (!ppNamespace) return WBEM_E_INVALID_PARAMETER;
    *ppNamespace = NULL;

    char *res = w2u(strNetworkResource);
    char *user = w2u(strUser);
    char *pass = w2u(strPassword);
    char *auth = w2u(strAuthority);   /* may carry "ntlmdomain:DOMAIN" or "kerberos:..." */

    IWbemServices *isvc = svc_create();
    if (!isvc) { free(res);free(user);free(pass);free(auth); return E_OUTOFMEMORY; }
    svc_t *s = (svc_t*)isvc;

    parse_resource(res, s->host, sizeof(s->host), s->ns, sizeof(s->ns));

    /* user may be "DOMAIN\\user"; split off the domain if present */
    s->domain[0]=0;
    {
        const char *bs = strchr(user, '\\');
        if (bs) {
            size_t dl=(size_t)(bs-user); if(dl>=sizeof(s->domain)) dl=sizeof(s->domain)-1;
            memcpy(s->domain, user, dl); s->domain[dl]=0;
            strncpy(s->user, bs+1, sizeof(s->user)-1); s->user[sizeof(s->user)-1]=0;
        } else {
            strncpy(s->user, user, sizeof(s->user)-1); s->user[sizeof(s->user)-1]=0;
        }
    }
    /* strAuthority "ntlmdomain:DOMAIN" overrides domain if user had none */
    if (!s->domain[0] && auth[0]) {
        const char *c = strchr(auth, ':');
        const char *d = c ? c+1 : auth;
        if (strncmp(auth,"ntlmdomain:",11)==0 || strncmp(auth,"kerberos:",9)==0) {
            strncpy(s->domain, d, sizeof(s->domain)-1); s->domain[sizeof(s->domain)-1]=0;
        }
    }
    strncpy(s->pass, pass, sizeof(s->pass)-1); s->pass[sizeof(s->pass)-1]=0;

    fac_log("facade: ConnectServer res=%s -> host=%s ns=%s user=%s domain=%s",
            res, s->host, s->ns, s->user, s->domain);

    free(res);free(user);free(pass);free(auth);

    /* We do NOT validate creds here (no round-trip); a bad host/cred surfaces at ExecQuery.
     * This mirrors how the real WbemScripting facade behaves for the probe. */
    *ppNamespace = isvc;
    return WBEM_S_NO_ERROR;
}

static const IWbemLocatorVtbl loc_vtbl = {
    loc_QI, loc_AddRef, loc_Release, loc_ConnectServer
};
static IWbemLocator *loc_create(void)
{
    loc_t *l=(loc_t*)calloc(1,sizeof(*l));
    if(!l) return NULL;
    l->base.lpVtbl=(IWbemLocatorVtbl*)&loc_vtbl;
    l->ref=1;
    InterlockedIncrement(&g_objs);
    return (IWbemLocator*)l;
}

/* ============================ IClassFactory ============================ */
typedef struct { IClassFactory base; LONG ref; } factory_t;

static HRESULT STDMETHODCALLTYPE cf_QI(IClassFactory *This, REFIID riid, void **ppv)
{
    if(!ppv) return E_POINTER;
    if(IsEqualGUID(riid,&g_IID_IUnknown)||IsEqualGUID(riid,&g_IID_IClassFactory)){
        *ppv=This; IClassFactory_AddRef(This); return S_OK;
    }
    *ppv=NULL; return E_NOINTERFACE;
}
static ULONG STDMETHODCALLTYPE cf_AddRef(IClassFactory *This)
{ factory_t *f=(factory_t*)This; return (ULONG)InterlockedIncrement(&f->ref); }
static ULONG STDMETHODCALLTYPE cf_Release(IClassFactory *This)
{ factory_t *f=(factory_t*)This; return (ULONG)InterlockedDecrement(&f->ref); }
static HRESULT STDMETHODCALLTYPE cf_CreateInstance(IClassFactory *This, IUnknown *pUnkOuter,
                                                   REFIID riid, void **ppv)
{
    (void)This;
    if(!ppv) return E_POINTER;
    *ppv=NULL;
    if(pUnkOuter) return CLASS_E_NOAGGREGATION;
    IWbemLocator *l = loc_create();
    if(!l) return E_OUTOFMEMORY;
    HRESULT hr = IWbemLocator_QueryInterface(l, riid, ppv);
    IWbemLocator_Release(l);
    return hr;
}
static HRESULT STDMETHODCALLTYPE cf_LockServer(IClassFactory *This, BOOL fLock)
{ (void)This; if(fLock) InterlockedIncrement(&g_locks); else InterlockedDecrement(&g_locks); return S_OK; }

static const IClassFactoryVtbl cf_vtbl = { cf_QI, cf_AddRef, cf_Release, cf_CreateInstance, cf_LockServer };
static factory_t g_factory = { { (IClassFactoryVtbl*)&cf_vtbl }, 1 };

/* ============================ DLL exports ============================ */
HRESULT WINAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void **ppv)
{
    if(!ppv) return E_POINTER;
    *ppv=NULL;
    if(IsEqualGUID(rclsid,&g_CLSID_WbemLocator)){
        return cf_QI((IClassFactory*)&g_factory, riid, ppv);
    }
    fac_log("facade: DllGetClassObject for unknown CLSID -> CLASS_E_CLASSNOTAVAILABLE");
    return CLASS_E_CLASSNOTAVAILABLE;
}

HRESULT WINAPI DllCanUnloadNow(void)
{
    return (g_objs == 0 && g_locks == 0) ? S_OK : S_FALSE;
}

/* Self-registration: repoint CLSID_WbemLocator's InprocServer32 at this DLL. */
static const WCHAR *CLSID_KEY =
    L"CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}\\InprocServer32";

HRESULT WINAPI DllRegisterServer(void)
{
    WCHAR path[MAX_PATH];
    HMODULE self = NULL;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)&DllRegisterServer, &self);
    if (!GetModuleFileNameW(self, path, MAX_PATH)) return E_FAIL;

    HKEY hk;
    if (RegCreateKeyExW(HKEY_CLASSES_ROOT, CLSID_KEY, 0, NULL, 0,
                        KEY_WRITE, NULL, &hk, NULL) != ERROR_SUCCESS)
        return E_FAIL;
    RegSetValueExW(hk, NULL, 0, REG_SZ, (const BYTE*)path,
                   (DWORD)((lstrlenW(path)+1)*sizeof(WCHAR)));
    RegSetValueExW(hk, L"ThreadingModel", 0, REG_SZ, (const BYTE*)L"Both", (DWORD)(5*sizeof(WCHAR)));
    RegCloseKey(hk);
    return S_OK;
}

HRESULT WINAPI DllUnregisterServer(void)
{
    /* Restore Wine's builtin wbemprox as the locator. */
    HKEY hk;
    if (RegCreateKeyExW(HKEY_CLASSES_ROOT, CLSID_KEY, 0, NULL, 0,
                        KEY_WRITE, NULL, &hk, NULL) == ERROR_SUCCESS) {
        const WCHAR *def = L"C:\\windows\\system32\\wbem\\wbemprox.dll";
        RegSetValueExW(hk, NULL, 0, REG_SZ, (const BYTE*)def,
                       (DWORD)((lstrlenW(def)+1)*sizeof(WCHAR)));
        RegCloseKey(hk);
    }
    return S_OK;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(hinst);
    return TRUE;
}
