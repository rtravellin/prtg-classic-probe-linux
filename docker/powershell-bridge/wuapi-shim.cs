// Interop.WUApiLib shim for Wine-Mono — makes PRTG's legacy LastWinUpdateXML.exe work.
//
// That helper uses the Windows Update Agent COM API (WUApiLib: UpdateSearcher.Search ->
// ISearchResult.Updates -> IUpdateCollection -> IUpdate.IsHidden/AutoSelectOnWebSites),
// which doesn't exist under Wine (and whose native remote-DCOM form Microsoft blocked).
// The helper references the UNSIGNED interop assembly "Interop.WUApiLib" (v4.0.0.0), so we
// just provide a managed assembly of the same name with the tiny surface it binds to, and
// route UpdateSearcher.Search() to the PSRP sidecar's /wua endpoint (which runs the WUA
// query on the target via a SYSTEM scheduled task — same mechanism as LastWindowsUpdateSensor).
// Parallel to the System.Management.Automation shim. See docker/ENGINE-C-POWERSHELL.md.
//
// Target host + creds come from the PRTG-injected environment (prtg_host / prtg_windowsuser /
// prtg_windowsdomain / prtg_windowspassword), which the helper's own process carries.
//
// Build:  mcs -target:library -out:Interop.WUApiLib.dll -r:System.dll wuapi-shim.cs
// (no strong name needed — the helper's reference has no public key token.)

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;

[assembly: System.Reflection.AssemblyVersion("4.0.0.0")]

namespace WUApiLib
{
    // Only the members LastWinUpdateXML actually calls.
    public interface IUpdate
    {
        bool IsHidden { get; }
        bool AutoSelectOnWebSites { get; }
        string Title { get; }
    }

    public interface IUpdateCollection : IEnumerable
    {
        int Count { get; }
        IUpdate this[int index] { get; }
        new IEnumerator GetEnumerator();   // declared on the interface itself (interop signature)
    }
    // coclass-interface alias — ISearchResult.Updates is typed as UpdateCollection
    public interface UpdateCollection : IUpdateCollection { }

    public interface ISearchResult
    {
        // interop signature returns the coclass-interface type UpdateCollection (not IUpdateCollection)
        UpdateCollection Updates { get; }
    }

    // COM interop flattens interface inheritance, so Search is exposed on
    // IUpdateSearcher3, and must be DECLARED on IUpdateSearcher3 itself.
    // The helper only uses IUpdateSearcher3 (+ the UpdateSearcher coclass-interface cast),
    // so keep it standalone — no base IUpdateSearcher/2 chain to avoid method-hiding pitfalls.
    public interface IUpdateSearcher3 { ISearchResult Search(string criteria); }
    public interface UpdateSearcher : IUpdateSearcher3 { }

    public interface IUpdateSession { IUpdateSearcher3 CreateUpdateSearcher(); }
    public interface UpdateSession : IUpdateSession { }

    // ---- coclasses (helper does `new UpdateSearcherClass()` / `new UpdateSessionClass()`) ----
    public class UpdateSearcherClass : UpdateSearcher
    {
        public ISearchResult Search(string criteria) { return WuaBridge.Search(criteria); }
    }

    public class UpdateSessionClass : UpdateSession
    {
        public IUpdateSearcher3 CreateUpdateSearcher() { return new UpdateSearcherClass(); }
    }

    // ---- backing implementations ----
    internal sealed class UpdateImpl : IUpdate
    {
        public bool IsHidden { get; internal set; }
        public bool AutoSelectOnWebSites { get; internal set; }
        public string Title { get; internal set; }
    }

    internal sealed class UpdateCollectionImpl : UpdateCollection
    {
        private readonly List<IUpdate> _items;
        public UpdateCollectionImpl(List<IUpdate> items) { _items = items; }
        public int Count { get { return _items.Count; } }
        public IUpdate this[int index] { get { return _items[index]; } }
        public IEnumerator GetEnumerator() { return _items.GetEnumerator(); }
    }

    internal sealed class SearchResultImpl : ISearchResult
    {
        private readonly UpdateCollection _updates;
        public SearchResultImpl(UpdateCollection u) { _updates = u; }
        public UpdateCollection Updates { get { return _updates; } }
    }

    // ---- bridge to the PSRP sidecar /wua endpoint ----
    internal static class WuaBridge
    {
        public static ISearchResult Search(string criteria)
        {
            string host = Env("prtg_host");
            string user = Env("prtg_windowsuser");
            string domain = Env("prtg_windowsdomain");
            string pass = Env("prtg_windowspassword");

            var sb = new StringBuilder("{");
            JS(sb, "host", host); sb.Append(',');
            JS(sb, "username", user); sb.Append(',');
            JS(sb, "domain", domain); sb.Append(',');
            JS(sb, "password", pass); sb.Append(',');
            JS(sb, "auth", "negotiate"); sb.Append(',');
            JS(sb, "criteria", criteria ?? "IsInstalled=0 or IsInstalled=1");
            sb.Append('}');

            string resp = Post(Endpoint("/wua"), sb.ToString());
            var map = Json.Parse(resp) as Dictionary<string, object>;
            var list = new List<IUpdate>();
            if (map != null)
            {
                object ok; map.TryGetValue("ok", out ok);
                if (!(ok is bool) || !(bool)ok)
                {
                    object err; map.TryGetValue("error", out err);
                    throw new Exception("wua bridge: " + (err != null ? err.ToString() : "failed"));
                }
                object objs; map.TryGetValue("objects", out objs);
                var rows = objs as List<object>;
                if (rows != null)
                    foreach (var o in rows)
                    {
                        var row = o as Dictionary<string, object>; if (row == null) continue;
                        list.Add(new UpdateImpl
                        {
                            IsHidden = B(row, "IsHidden"),
                            AutoSelectOnWebSites = B(row, "AutoSelectOnWebSites"),
                            Title = S(row, "Title"),
                        });
                    }
            }
            return new SearchResultImpl(new UpdateCollectionImpl(list));
        }

        private static bool B(Dictionary<string, object> row, string name)
        {
            object cell; if (!row.TryGetValue(name, out cell)) return false;
            var c = cell as Dictionary<string, object>; if (c == null) return false;
            object v; c.TryGetValue("v", out v);
            if (v is bool) return (bool)v;
            string s = v == null ? "" : v.ToString();
            return s == "1" || s.Equals("true", StringComparison.OrdinalIgnoreCase);
        }
        private static string S(Dictionary<string, object> row, string name)
        {
            object cell; if (!row.TryGetValue(name, out cell)) return "";
            var c = cell as Dictionary<string, object>; if (c == null) return "";
            object v; c.TryGetValue("v", out v); return v == null ? "" : v.ToString();
        }

        private static string Env(string n) { var v = Environment.GetEnvironmentVariable(n); return v ?? ""; }
        private static string Endpoint(string path)
        {
            string h = Environment.GetEnvironmentVariable("PSRP_BRIDGE_ADDR"); if (string.IsNullOrEmpty(h)) h = "127.0.0.1";
            string pt = Environment.GetEnvironmentVariable("PSRP_BRIDGE_PORT"); if (string.IsNullOrEmpty(pt)) pt = "8911";
            return "http://" + h + ":" + pt + path;
        }
        private static void JS(StringBuilder sb, string k, string v)
        {
            sb.Append('"').Append(k).Append("\":");
            if (v == null) { sb.Append("null"); return; }
            sb.Append('"');
            foreach (char ch in v)
            {
                if (ch == '"') sb.Append("\\\"");
                else if (ch == '\\') sb.Append("\\\\");
                else if (ch == '\n') sb.Append("\\n");
                else if (ch == '\r') sb.Append("\\r");
                else if (ch == '\t') sb.Append("\\t");
                else if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                else sb.Append(ch);
            }
            sb.Append('"');
        }
        private static string Post(string url, string body)
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "POST"; req.ContentType = "application/json"; req.Timeout = 300000;
            string tok = Environment.GetEnvironmentVariable("PSRP_BRIDGE_TOKEN");
            if (!string.IsNullOrEmpty(tok)) req.Headers.Add("X-Bridge-Token", tok);
            byte[] data = Encoding.UTF8.GetBytes(body); req.ContentLength = data.Length;
            using (var s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var rd = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return rd.ReadToEnd();
        }
    }

    // minimal JSON parser (object/array/string/number/bool/null)
    internal static class Json
    {
        public static object Parse(string s) { int i = 0; return PV(s, ref i); }
        private static void W(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }
        private static object PV(string s, ref int i)
        {
            W(s, ref i); char c = s[i];
            if (c == '{') return PO(s, ref i);
            if (c == '[') return PA(s, ref i);
            if (c == '"') return PS(s, ref i);
            if (c == 't') { i += 4; return true; }
            if (c == 'f') { i += 5; return false; }
            if (c == 'n') { i += 4; return null; }
            return PN(s, ref i);
        }
        private static Dictionary<string, object> PO(string s, ref int i)
        {
            var d = new Dictionary<string, object>(); i++; W(s, ref i);
            if (s[i] == '}') { i++; return d; }
            while (true) { W(s, ref i); string k = PS(s, ref i); W(s, ref i); i++; d[k] = PV(s, ref i); W(s, ref i); if (s[i] == ',') { i++; continue; } i++; break; }
            return d;
        }
        private static List<object> PA(string s, ref int i)
        {
            var l = new List<object>(); i++; W(s, ref i);
            if (s[i] == ']') { i++; return l; }
            while (true) { l.Add(PV(s, ref i)); W(s, ref i); if (s[i] == ',') { i++; continue; } i++; break; }
            return l;
        }
        private static string PS(string s, ref int i)
        {
            var sb = new StringBuilder(); i++;
            while (s[i] != '"')
            {
                char c = s[i++];
                if (c == '\\') { char e = s[i++]; switch (e) { case 'n': sb.Append('\n'); break; case 'r': sb.Append('\r'); break; case 't': sb.Append('\t'); break; case '"': sb.Append('"'); break; case '\\': sb.Append('\\'); break; case '/': sb.Append('/'); break; case 'u': sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16)); i += 4; break; default: sb.Append(e); break; } }
                else sb.Append(c);
            }
            i++; return sb.ToString();
        }
        private static object PN(string s, ref int i)
        {
            int st = i; while (i < s.Length && (char.IsDigit(s[i]) || "+-.eE".IndexOf(s[i]) >= 0)) i++;
            string n = s.Substring(st, i - st); long l; if (long.TryParse(n, out l)) return l;
            return double.Parse(n, System.Globalization.CultureInfo.InvariantCulture);
        }
    }
}
