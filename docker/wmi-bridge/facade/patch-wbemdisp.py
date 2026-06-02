#!/usr/bin/env python3
"""
Patch Wine's dlls/wbemdisp/locator.c to implement propertyset_get__NewEnum.

Wine 11.0 ships this as a `FIXME; return E_NOTIMPL` stub, which breaks
`For Each prop In obj.Properties_` — the exact pattern the probe's WMI dataset reader
uses to read a WMI object's columns. We implement it by enumerating the underlying
IWbemClassObject's property names (IWbemClassObject::GetNames) and returning an
IEnumVARIANT of ISWbemProperty objects (built with the file-local SWbemProperty_create,
the same helper propertyset_Item already uses). See docker/WMI-FACADE.md §"propertyset".

Idempotent-ish: refuses to run if the stub marker is gone.
"""
import re, sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "dlls/wbemdisp/locator.c"
src = open(PATH, encoding="utf-8", errors="surrogateescape").read()

if "propertyset_get__NewEnum" not in src:
    sys.exit("ERROR: propertyset_get__NewEnum not found — wrong file/version")

if "facade_propenum" in src:
    print("already patched — nothing to do")
    sys.exit(0)

# Locate the function and capture its exact signature (param names vary by version).
m = re.search(
    r"static HRESULT WINAPI propertyset_get__NewEnum\((?P<sig>[^)]*)\)\s*\{(?P<body>.*?)\n\}",
    src, re.DOTALL)
if not m:
    sys.exit("ERROR: could not match propertyset_get__NewEnum function body")
if "E_NOTIMPL" not in m.group("body"):
    sys.exit("ERROR: propertyset_get__NewEnum is not the E_NOTIMPL stub (already patched?)")

# derive the iface param name (first arg, last token) and out param (second arg, last token)
sig = m.group("sig")
args = [a.strip() for a in sig.split(",")]
iface_name = re.split(r"[ \t*]+", args[0].strip())[-1]
out_name   = re.split(r"[ \t*]+", args[1].strip())[-1]

IMPL = r"""
/* --- PRTG facade patch: implement property-set enumeration (Wine ships a stub) --- */
struct facade_propenum
{
    IEnumVARIANT IEnumVARIANT_iface;
    LONG refs;
    ISWbemProperty **items;
    ULONG count;
    ULONG index;
};

static struct facade_propenum *impl_from_facade_propenum( IEnumVARIANT *iface )
{
    return CONTAINING_RECORD( iface, struct facade_propenum, IEnumVARIANT_iface );
}

static HRESULT WINAPI facade_propenum_QueryInterface( IEnumVARIANT *iface, REFIID riid, void **obj )
{
    if (IsEqualGUID( riid, &IID_IUnknown ) || IsEqualGUID( riid, &IID_IEnumVARIANT ))
    {
        *obj = iface;
        IEnumVARIANT_AddRef( iface );
        return S_OK;
    }
    *obj = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI facade_propenum_AddRef( IEnumVARIANT *iface )
{
    struct facade_propenum *e = impl_from_facade_propenum( iface );
    return InterlockedIncrement( &e->refs );
}

static ULONG WINAPI facade_propenum_Release( IEnumVARIANT *iface )
{
    struct facade_propenum *e = impl_from_facade_propenum( iface );
    LONG refs = InterlockedDecrement( &e->refs );
    if (!refs)
    {
        ULONG i;
        for (i = 0; i < e->count; i++)
            if (e->items[i]) ISWbemProperty_Release( e->items[i] );
        free( e->items );
        free( e );
    }
    return refs;
}

static HRESULT WINAPI facade_propenum_Next( IEnumVARIANT *iface, ULONG celt, VARIANT *var, ULONG *fetched )
{
    struct facade_propenum *e = impl_from_facade_propenum( iface );
    ULONG got = 0;
    while (got < celt && e->index < e->count)
    {
        /* ISWbemProperty is a dispinterface deriving from IDispatch (IDispatch methods sit
         * at the head of its vtable), so the pointer IS a valid IDispatch* — cast + AddRef
         * directly rather than QueryInterface (Wine's property QI declines IID_IDispatch). */
        VariantInit( &var[got] );
        V_VT( &var[got] ) = VT_DISPATCH;
        V_DISPATCH( &var[got] ) = (IDispatch *)e->items[e->index];
        IDispatch_AddRef( V_DISPATCH( &var[got] ) );
        e->index++;
        got++;
    }
    if (fetched) *fetched = got;
    return (got < celt) ? S_FALSE : S_OK;
}

static HRESULT WINAPI facade_propenum_Skip( IEnumVARIANT *iface, ULONG celt )
{
    struct facade_propenum *e = impl_from_facade_propenum( iface );
    while (celt && e->index < e->count) { e->index++; celt--; }
    return celt ? S_FALSE : S_OK;
}

static HRESULT WINAPI facade_propenum_Reset( IEnumVARIANT *iface )
{
    struct facade_propenum *e = impl_from_facade_propenum( iface );
    e->index = 0;
    return S_OK;
}

static HRESULT WINAPI facade_propenum_Clone( IEnumVARIANT *iface, IEnumVARIANT **out )
{
    if (out) *out = NULL;
    return E_NOTIMPL;
}

static const IEnumVARIANTVtbl facade_propenum_vtbl =
{
    facade_propenum_QueryInterface,
    facade_propenum_AddRef,
    facade_propenum_Release,
    facade_propenum_Next,
    facade_propenum_Skip,
    facade_propenum_Reset,
    facade_propenum_Clone,
};

static HRESULT WINAPI propertyset_get__NewEnum( ISWbemPropertySet *__IFACE__, IUnknown **__OUT__ )
{
    struct propertyset *propertyset = impl_from_ISWbemPropertySet( __IFACE__ );
    struct facade_propenum *e;
    SAFEARRAY *names = NULL;
    LONG lb = 0, ub = -1, i;
    ULONG n = 0;
    HRESULT hr;

    TRACE( "%p, %p\n", propertyset, __OUT__ );
    if (!__OUT__) return E_POINTER;
    *__OUT__ = NULL;

    hr = IWbemClassObject_GetNames( propertyset->object, NULL,
                                    WBEM_FLAG_ALWAYS | WBEM_FLAG_NONSYSTEM_ONLY, NULL, &names );
    if (FAILED( hr )) return hr;

    SafeArrayGetLBound( names, 1, &lb );
    SafeArrayGetUBound( names, 1, &ub );

    if (!(e = malloc( sizeof(*e) ))) { SafeArrayDestroy( names ); return E_OUTOFMEMORY; }
    e->IEnumVARIANT_iface.lpVtbl = &facade_propenum_vtbl;
    e->refs = 1;
    e->index = 0;
    e->count = 0;
    e->items = (ub >= lb) ? calloc( ub - lb + 1, sizeof(*e->items) ) : NULL;

    for (i = lb; i <= ub; i++)
    {
        BSTR name = NULL;
        ISWbemProperty *prop = NULL;
        if (FAILED( SafeArrayGetElement( names, &i, &name ) )) continue;
        if (SUCCEEDED( SWbemProperty_create( propertyset->object, name, &prop ) ))
            e->items[n++] = prop;
        SysFreeString( name );
    }
    e->count = n;
    SafeArrayDestroy( names );

    *__OUT__ = (IUnknown *)&e->IEnumVARIANT_iface;
    return S_OK;
}
"""

IMPL = IMPL.replace("__IFACE__", iface_name).replace("__OUT__", out_name)

src = src[:m.start()] + IMPL.strip("\n") + src[m.end():]

# Ensure SWbemProperty_create is declared before our use (it is defined earlier in the
# file, but add a forward declaration right after the includes to be safe).
fwd = "static HRESULT SWbemProperty_create( IWbemClassObject *, BSTR, ISWbemProperty ** );\n"
if "SWbemProperty_create( IWbemClassObject *, BSTR, ISWbemProperty ** );" not in src:
    # insert after the last #include near the top
    incs = list(re.finditer(r"#include[^\n]*\n", src))
    pos = incs[-1].end() if incs else 0
    src = src[:pos] + "\n" + fwd + src[pos:]

# --- implement the ISWbemProperty getters Wine ships as FIXME/E_NOTIMPL stubs ---
# The probe reads Name/CIMType/IsArray (and IsLocal/Origin) for each enumerated property to
# build the WMI dataset's columns. All are derivable from struct property {object, name} via
# IWbemClassObject::Get's CIMTYPE out-param. (Value is already implemented upstream.)
def replace_stub(src, fname, impl):
    m = re.search(
        r"static HRESULT WINAPI %s\([^)]*\)\s*\{(?P<body>.*?)\n\}" % re.escape(fname),
        src, re.DOTALL)
    if not m:
        print("  %s: not found (skipped)" % fname); return src
    if "FIXME" not in m.group("body") and "E_NOTIMPL" not in m.group("body"):
        print("  %s: already implemented" % fname); return src
    print("  %s: patched" % fname)
    return src[:m.start()] + impl.strip("\n") + src[m.end():]

PROP_IMPLS = {
"property_get_Name": r"""
static HRESULT WINAPI property_get_Name( ISWbemProperty *iface, BSTR *str )
{
    struct property *property = impl_from_ISWbemProperty( iface );
    TRACE( "%p, %p\n", property, str );
    if (!str) return E_POINTER;
    *str = SysAllocStringLen( property->name, SysStringLen( property->name ) );
    return *str ? S_OK : E_OUTOFMEMORY;
}""",
"property_get_CIMType": r"""
static HRESULT WINAPI property_get_CIMType( ISWbemProperty *iface, WbemCimtypeEnum *type )
{
    struct property *property = impl_from_ISWbemProperty( iface );
    VARIANT var; CIMTYPE ct = 0; HRESULT hr;
    TRACE( "%p, %p\n", property, type );
    if (!type) return E_POINTER;
    hr = IWbemClassObject_Get( property->object, property->name, 0, &var, &ct, NULL );
    if (FAILED(hr)) return hr;
    VariantClear( &var );
    *type = ct & ~CIM_FLAG_ARRAY;
    return S_OK;
}""",
"property_get_IsArray": r"""
static HRESULT WINAPI property_get_IsArray( ISWbemProperty *iface, VARIANT_BOOL *array )
{
    struct property *property = impl_from_ISWbemProperty( iface );
    VARIANT var; CIMTYPE ct = 0; HRESULT hr;
    TRACE( "%p, %p\n", property, array );
    if (!array) return E_POINTER;
    hr = IWbemClassObject_Get( property->object, property->name, 0, &var, &ct, NULL );
    if (FAILED(hr)) return hr;
    VariantClear( &var );
    *array = (ct & CIM_FLAG_ARRAY) ? VARIANT_TRUE : VARIANT_FALSE;
    return S_OK;
}""",
"property_get_IsLocal": r"""
static HRESULT WINAPI property_get_IsLocal( ISWbemProperty *iface, VARIANT_BOOL *local )
{
    TRACE( "%p, %p\n", iface, local );
    if (!local) return E_POINTER;
    *local = VARIANT_TRUE;
    return S_OK;
}""",
"property_get_Origin": r"""
static HRESULT WINAPI property_get_Origin( ISWbemProperty *iface, BSTR *origin )
{
    TRACE( "%p, %p\n", iface, origin );
    if (!origin) return E_POINTER;
    *origin = SysAllocString( L"" );
    return *origin ? S_OK : E_OUTOFMEMORY;
}""",
}
for fname, impl in PROP_IMPLS.items():
    src = replace_stub(src, fname, impl)

# --- implement ISWbemQualifierSet (Wine leaves *_get_Qualifiers_ as E_NOTIMPL stubs and
# has no qualifier-set object at all). The probe reads each property's Qualifiers_ while
# building its WMI dataset; an empty-but-enumerable set lets it proceed (it types columns
# from ISWbemProperty.CIMType, which we implemented above). Registered as a real
# dispinterface so `For Each q In prop.Qualifiers_` works via the typelib. ---
if "facade_qualifierset" not in src:
    # 1) register ISWbemQualifierSet_tid in the type_id enum + GUID table (same index)
    src = src.replace("    ISWbemMethod_tid,\n    last_tid",
                      "    ISWbemMethod_tid,\n    ISWbemQualifierSet_tid,\n    last_tid", 1)
    src = src.replace("    &IID_ISWbemMethod,\n};",
                      "    &IID_ISWbemMethod,\n    &IID_ISWbemQualifierSet,\n};", 1)

    QSET = r"""
/* --- PRTG facade patch: minimal empty ISWbemQualifierSet + its IEnumVARIANT --- */
struct facade_emptyenum { IEnumVARIANT IEnumVARIANT_iface; LONG refs; };
static struct facade_emptyenum *impl_from_facade_emptyenum( IEnumVARIANT *iface )
{ return CONTAINING_RECORD( iface, struct facade_emptyenum, IEnumVARIANT_iface ); }
static HRESULT WINAPI fee_QI( IEnumVARIANT *iface, REFIID riid, void **obj )
{ if (IsEqualGUID(riid,&IID_IUnknown)||IsEqualGUID(riid,&IID_IEnumVARIANT)){ *obj=iface; IEnumVARIANT_AddRef(iface); return S_OK; } *obj=NULL; return E_NOINTERFACE; }
static ULONG WINAPI fee_AddRef( IEnumVARIANT *iface ){ return InterlockedIncrement( &impl_from_facade_emptyenum(iface)->refs ); }
static ULONG WINAPI fee_Release( IEnumVARIANT *iface ){ struct facade_emptyenum *e=impl_from_facade_emptyenum(iface); LONG r=InterlockedDecrement(&e->refs); if(!r) free(e); return r; }
static HRESULT WINAPI fee_Next( IEnumVARIANT *iface, ULONG celt, VARIANT *var, ULONG *fetched ){ if(fetched)*fetched=0; return S_FALSE; }
static HRESULT WINAPI fee_Skip( IEnumVARIANT *iface, ULONG celt ){ return S_FALSE; }
static HRESULT WINAPI fee_Reset( IEnumVARIANT *iface ){ return S_OK; }
static HRESULT WINAPI fee_Clone( IEnumVARIANT *iface, IEnumVARIANT **out ){ if(out)*out=NULL; return E_NOTIMPL; }
static const IEnumVARIANTVtbl facade_emptyenum_vtbl = { fee_QI, fee_AddRef, fee_Release, fee_Next, fee_Skip, fee_Reset, fee_Clone };
static IUnknown *facade_create_emptyenum(void)
{ struct facade_emptyenum *e = malloc(sizeof(*e)); if(!e) return NULL; e->IEnumVARIANT_iface.lpVtbl=&facade_emptyenum_vtbl; e->refs=1; return (IUnknown*)&e->IEnumVARIANT_iface; }

struct facade_qualifierset { ISWbemQualifierSet ISWbemQualifierSet_iface; LONG refs; };
static struct facade_qualifierset *impl_from_facade_qualifierset( ISWbemQualifierSet *iface )
{ return CONTAINING_RECORD( iface, struct facade_qualifierset, ISWbemQualifierSet_iface ); }
static HRESULT WINAPI fqs_QI( ISWbemQualifierSet *iface, REFIID riid, void **obj )
{ if (IsEqualGUID(riid,&IID_IUnknown)||IsEqualGUID(riid,&IID_IDispatch)||IsEqualGUID(riid,&IID_ISWbemQualifierSet)){ *obj=iface; ISWbemQualifierSet_AddRef(iface); return S_OK; } *obj=NULL; return E_NOINTERFACE; }
static ULONG WINAPI fqs_AddRef( ISWbemQualifierSet *iface ){ return InterlockedIncrement( &impl_from_facade_qualifierset(iface)->refs ); }
static ULONG WINAPI fqs_Release( ISWbemQualifierSet *iface ){ struct facade_qualifierset *q=impl_from_facade_qualifierset(iface); LONG r=InterlockedDecrement(&q->refs); if(!r) free(q); return r; }
static HRESULT WINAPI fqs_GetTypeInfoCount( ISWbemQualifierSet *iface, UINT *count ){ *count=1; return S_OK; }
static HRESULT WINAPI fqs_GetTypeInfo( ISWbemQualifierSet *iface, UINT index, LCID lcid, ITypeInfo **info ){ return get_typeinfo( ISWbemQualifierSet_tid, info ); }
static HRESULT WINAPI fqs_GetIDsOfNames( ISWbemQualifierSet *iface, REFIID riid, LPOLESTR *names, UINT count, LCID lcid, DISPID *dispid )
{ ITypeInfo *ti; HRESULT hr = get_typeinfo( ISWbemQualifierSet_tid, &ti ); if (SUCCEEDED(hr)){ hr = ITypeInfo_GetIDsOfNames( ti, names, count, dispid ); ITypeInfo_Release(ti); } return hr; }
static HRESULT WINAPI fqs_Invoke( ISWbemQualifierSet *iface, DISPID member, REFIID riid, LCID lcid, WORD flags, DISPPARAMS *params, VARIANT *result, EXCEPINFO *ei, UINT *arg_err )
{ ITypeInfo *ti; HRESULT hr = get_typeinfo( ISWbemQualifierSet_tid, &ti ); if (SUCCEEDED(hr)){ hr = ITypeInfo_Invoke( ti, iface, member, flags, params, result, ei, arg_err ); ITypeInfo_Release(ti); } return hr; }
static HRESULT WINAPI fqs_get__NewEnum( ISWbemQualifierSet *iface, IUnknown **unk ){ if(!unk) return E_POINTER; *unk = facade_create_emptyenum(); return *unk ? S_OK : E_OUTOFMEMORY; }
static HRESULT WINAPI fqs_Item( ISWbemQualifierSet *iface, BSTR name, LONG flags, ISWbemQualifier **out ){ if(out)*out=NULL; return WBEM_E_NOT_FOUND; }
static HRESULT WINAPI fqs_get_Count( ISWbemQualifierSet *iface, LONG *count ){ if(count)*count=0; return S_OK; }
static HRESULT WINAPI fqs_Add( ISWbemQualifierSet *iface, BSTR n, VARIANT *v, VARIANT_BOOL a, VARIANT_BOOL b, VARIANT_BOOL c, LONG f, ISWbemQualifier **out ){ if(out)*out=NULL; return E_NOTIMPL; }
static HRESULT WINAPI fqs_Remove( ISWbemQualifierSet *iface, BSTR n, LONG f ){ return E_NOTIMPL; }
static const ISWbemQualifierSetVtbl facade_qualifierset_vtbl = {
    fqs_QI, fqs_AddRef, fqs_Release, fqs_GetTypeInfoCount, fqs_GetTypeInfo, fqs_GetIDsOfNames, fqs_Invoke,
    fqs_get__NewEnum, fqs_Item, fqs_get_Count, fqs_Add, fqs_Remove };
static HRESULT facade_create_qualifierset( ISWbemQualifierSet **out )
{ struct facade_qualifierset *q; if (!out) return E_POINTER; if (!(q = malloc(sizeof(*q)))) return E_OUTOFMEMORY;
  q->ISWbemQualifierSet_iface.lpVtbl = &facade_qualifierset_vtbl; q->refs=1; *out = &q->ISWbemQualifierSet_iface; return S_OK; }

"""
    # insert the machinery right before property_get_Qualifiers_ (its earliest use)
    anchor = "static HRESULT WINAPI property_get_Qualifiers_("
    idx = src.find(anchor)
    if idx < 0:
        sys.exit("ERROR: could not find property_get_Qualifiers_ to anchor qualifier set")
    src = src[:idx] + QSET.lstrip("\n") + src[idx:]

    # wire all three *_get_Qualifiers_ stubs to return an empty qualifier set
    def wire_qualifiers(src, fname):
        m = re.search(r"static HRESULT WINAPI %s\((?P<sig>.*?)\)\s*\{(?P<body>.*?)\n\}" % re.escape(fname),
                      src, re.DOTALL)
        if not m:
            print("  %s: not found" % fname); return src
        out = re.split(r"[ \t\n*]+", [a for a in m.group("sig").split(",")][-1].strip())[-1]
        impl = ("static HRESULT WINAPI %s(%s)\n{\n    return facade_create_qualifierset( %s );\n}"
                % (fname, m.group("sig"), out))
        print("  %s: wired to empty qualifier set (out=%s)" % (fname, out))
        return src[:m.start()] + impl + src[m.end():]
    for fn in ("property_get_Qualifiers_", "object_get_Qualifiers_", "method_get_Qualifiers_"):
        src = wire_qualifiers(src, fn)
    print("added ISWbemQualifierSet")

open(PATH, "w", encoding="utf-8", errors="surrogateescape").write(src)
print("patched propertyset_get__NewEnum (iface=%s out=%s)" % (iface_name, out_name))
