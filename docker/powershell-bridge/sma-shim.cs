// System.Management.Automation shim for Wine-Mono — PSRP bridge for PRTG's .NET
// PowerShell "Engine C" helpers (ExchangeSensorPS.exe, SCVMMSensor.exe).
//
// Wine-Mono ships no System.Management.Automation, so these helpers can't run. They are
// PSRP *clients* (WSManConnectionInfo -> RunspaceFactory.CreateRunspace -> Pipeline.AddScript/
// Invoke -> read PSObject.Properties); the Exchange/VMM cmdlets execute on the remote server.
// This assembly reimplements ONLY the narrow client surface the helpers bind to, and routes
// Pipeline.Invoke() to the psrp-sidecar.py (pypsrp) over HTTP, which does the real PSRP/WSMan
// call with NTLM. Direct analogue of wbemfacade.dll + wmi-sidecar.py for WMI. See
// docker/ENGINE-C-POWERSHELL.md.
//
// Build (Wine-Mono mcs):  see build-sma-shim.sh
// Identity must match the helpers' reference: System.Management.Automation,
// Version=3.0.0.0, PublicKeyToken=31bf3856ad364e35 (delay-signed; Mono skips SN verify).

using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;

[assembly: System.Reflection.AssemblyVersion("3.0.0.0")]

// ----------------------------------------------------------------------------- core
namespace System.Management.Automation
{
    // Base of all PSObject members. Helpers use .Name and .Value (object).
    public abstract class PSMemberInfo
    {
        public string Name { get; protected set; }
        public virtual object Value { get; set; }
    }

    public class PSPropertyInfo : PSMemberInfo
    {
        // NB: do NOT re-declare Value here — overriding it with a second auto-property
        // gives a separate backing field, so a ctor that sets one and a getter that
        // reads the other silently return null. Inherit PSMemberInfo.Value (one field).
        public PSPropertyInfo(string name, object value) { Name = name; Value = value; }
    }

    // Helpers index by name (obj.Properties["X"]) and foreach over it. Missing name -> null.
    public class PSMemberInfoCollection<T> : IEnumerable<T> where T : PSMemberInfo
    {
        private readonly List<T> _items = new List<T>();
        private readonly Dictionary<string, T> _byName =
            new Dictionary<string, T>(StringComparer.OrdinalIgnoreCase);

        internal void AddInternal(T item)
        {
            _items.Add(item);
            if (item != null && item.Name != null) _byName[item.Name] = item;
        }
        public T this[string name]
        {
            get { T v; return (name != null && _byName.TryGetValue(name, out v)) ? v : null; }
        }
        public IEnumerator<T> GetEnumerator() { return _items.GetEnumerator(); }
        IEnumerator IEnumerable.GetEnumerator() { return _items.GetEnumerator(); }
    }

    public class PSObject
    {
        private readonly PSMemberInfoCollection<PSPropertyInfo> _props =
            new PSMemberInfoCollection<PSPropertyInfo>();
        private readonly PSMemberInfoCollection<PSMemberInfo> _members =
            new PSMemberInfoCollection<PSMemberInfo>();

        public PSObject() { }
        public PSMemberInfoCollection<PSPropertyInfo> Properties { get { return _props; } }
        public PSMemberInfoCollection<PSMemberInfo> Members { get { return _members; } }
        // NB: this field name is part of the binary-compatible surface the .NET helpers
        // expect; keep it exactly so a deserialized object reports correctly whether it
        // actually wrapped a collection.
        private bool immediateBaseObjectIsEmpty = true;
        private object _base;
        public object ImmediateBaseObject
        {
            get { return _base; }
            internal set { _base = value; immediateBaseObjectIsEmpty = (value == null); }
        }
        public object BaseObject { get { return ImmediateBaseObject; } }

        internal void AddProperty(string name, object value)
        {
            var p = new PSPropertyInfo(name, value);
            _props.AddInternal(p);
            _members.AddInternal(p);
        }
        public override string ToString()
        {
            return ImmediateBaseObject != null ? ImmediateBaseObject.ToString() : base.ToString();
        }
        public string ToString(string format, IFormatProvider formatProvider) { return ToString(); }
    }

    public sealed class PSCredential
    {
        public PSCredential(string userName, SecureString password)
        { UserName = userName; Password = password; }
        public string UserName { get; private set; }
        public SecureString Password { get; private set; }
        public NetworkCredential GetNetworkCredential()
        { return new NetworkCredential(UserName, Bridge.Unprotect(Password)); }
    }

    // Exception types the helpers catch.
    public class RuntimeException : Exception
    { public RuntimeException() { } public RuntimeException(string m) : base(m) { } public RuntimeException(string m, Exception i) : base(m, i) { } }
    public class RemoteException : RuntimeException
    { public RemoteException() { } public RemoteException(string m) : base(m) { } public RemoteException(string m, Exception i) : base(m, i) { } }
    public class CmdletInvocationException : RuntimeException
    { public CmdletInvocationException(string m) : base(m) { } }
}

namespace System.Management.Automation.Runspaces
{
    public enum AuthenticationMechanism
    { Default, Basic, Negotiate, NegotiateWithImplicitCredential, Credssp, Digest, Kerberos }

    public enum RunspaceState
    { BeforeOpen, Opening, Opened, Closed, Closing, Broken, Disconnecting, Disconnected, Connecting }

    public sealed class RunspaceStateInfo
    {
        public RunspaceStateInfo(RunspaceState state, Exception reason) { State = state; Reason = reason; }
        public RunspaceState State { get; private set; }
        public Exception Reason { get; private set; }
        public override string ToString() { return State.ToString(); }
    }

    public sealed class RunspaceStateEventArgs : EventArgs
    {
        public RunspaceStateEventArgs(RunspaceStateInfo info) { RunspaceStateInfo = info; }
        public RunspaceStateInfo RunspaceStateInfo { get; private set; }
    }

    // Base class. In real SMA these settable knobs live on RunspaceConnectionInfo, and
    // the helpers bind these setters to THIS type, so the property surface must match.
    public abstract class RunspaceConnectionInfo
    {
        public AuthenticationMechanism AuthenticationMechanism { get; set; }
        public PSCredential Credential { get; set; }
        public int MaximumConnectionRedirectionCount { get; set; }
        public int OpenTimeout { get; set; }
        public int OperationTimeout { get; set; }
        public int IdleTimeout { get; set; }
        public int CancelTimeout { get; set; }
        public string Culture { get; set; }
        public string UICulture { get; set; }
    }

    public sealed class WSManConnectionInfo : RunspaceConnectionInfo
    {
        public WSManConnectionInfo(bool useSsl, string computerName, int port, string appName,
                                   string shellUri, PSCredential credential)
        {
            UseSsl = useSsl; ComputerName = computerName; Port = port;
            AppName = appName; ShellUri = shellUri; Credential = credential;
        }
        public bool UseSsl { get; set; }
        public string ComputerName { get; set; }
        public int Port { get; set; }
        public string AppName { get; set; }
        public string ShellUri { get; set; }
        public bool IncludePortInSPN { get; set; }
        public bool SkipCACheck { get; set; }
        public bool SkipCNCheck { get; set; }
        public bool SkipRevocationCheck { get; set; }
        public int MaxConnectionRetryCount { get; set; }
        public bool NoEncryption { get; set; }
        public bool NoMachineProfile { get; set; }
        public Uri ConnectionUri { get; set; }
    }

    public static class RunspaceFactory
    {
        public static Runspace CreateRunspace(RunspaceConnectionInfo connectionInfo)
        { return new Runspace((WSManConnectionInfo)connectionInfo); }
        // No-arg = a LOCAL runspace. Some helpers reference it in an unused "localhost"
        // branch (e.g. LastWindowsUpdateSensor); it must EXIST so Mono can JIT the method,
        // but a local runspace has no meaning under Wine — it errors only if actually used.
        public static Runspace CreateRunspace() { return new Runspace(null); }
    }

    public sealed class Runspace : IDisposable
    {
        internal WSManConnectionInfo Conn;
        private RunspaceStateInfo _state = new RunspaceStateInfo(RunspaceState.BeforeOpen, null);
        public event EventHandler<RunspaceStateEventArgs> StateChanged;

        internal Runspace(WSManConnectionInfo conn) { Conn = conn; }
        public RunspaceStateInfo RunspaceStateInfo { get { return _state; } }
        // NOTE: real SMA exposes Runspace.Version (engine version). The PowerShell-based WU
        // helper's "Powershell version not supported" gate does NOT read it here — it reads the
        // version from a separate local property that the shim returns empty, so the full
        // spawned-helper flow stops at that gate under Wine. For Windows monitoring on the probe,
        // use the native WMI sensors instead.

        private void SetState(RunspaceState s, Exception ex)
        {
            _state = new RunspaceStateInfo(s, ex);
            var h = StateChanged;
            if (h != null) h(this, new RunspaceStateEventArgs(_state));
        }
        // No persistent connection is opened here; the sidecar opens a runspace per Invoke.
        // We still fire the state transitions the helpers watch.
        public void Open() { SetState(RunspaceState.Opening, null); SetState(RunspaceState.Opened, null); }
        public void Close() { SetState(RunspaceState.Closing, null); SetState(RunspaceState.Closed, null); }
        public void Dispose() { try { Close(); } catch { } }
        public Pipeline CreatePipeline() { return new Pipeline(this); }
    }

    public sealed class Command
    {
        public Command(string command) { CommandText = command; }
        public Command(string command, bool isScript) { CommandText = command; IsScript = isScript; }
        public string CommandText { get; private set; }
        public bool IsScript { get; private set; }
        public CommandParameterCollection Parameters { get; private set; } = new CommandParameterCollection();
    }

    public sealed class CommandParameter
    {
        public CommandParameter(string name) { Name = name; }
        public CommandParameter(string name, object value) { Name = name; Value = value; }
        public string Name { get; private set; }
        public object Value { get; private set; }
    }

    public sealed class CommandParameterCollection : Collection<CommandParameter> { }

    // Commands.AddScript(...) and Commands.Add("Name") and Add(Command).
    public sealed class CommandCollection : Collection<Command>
    {
        public void AddScript(string script) { Add(new Command(script, true)); }
        public void AddScript(string script, bool useLocalScope) { Add(new Command(script, true)); }
        public void Add(string command) { Add(new Command(command)); }
    }

    public sealed class Pipeline
    {
        private readonly Runspace _rs;
        private readonly CommandCollection _commands = new CommandCollection();
        internal Pipeline(Runspace rs) { _rs = rs; }
        public CommandCollection Commands { get { return _commands; } }

        public Collection<PSObject> Invoke()
        {
            return Bridge.InvokePipeline(_rs.Conn, _commands);
        }
    }
}

// PSRemotingTransportException lives in System.Management.Automation.Remoting — the
// helpers catch it by that fully-qualified name around Runspace.Open()/RunCommand().
namespace System.Management.Automation.Remoting
{
    public class PSRemotingTransportException : Exception
    {
        public PSRemotingTransportException() { }
        public PSRemotingTransportException(string m) : base(m) { }
        public PSRemotingTransportException(string m, Exception i) : base(m, i) { }
    }
}

// ----------------------------------------------------------------------------- bridge
namespace System.Management.Automation
{
    using System.Management.Automation.Runspaces;
    using System.Management.Automation.Remoting;

    // High-level PowerShell API (PowerShell.Create().AddScript(...).Invoke()) some helpers
    // use instead of Runspace.CreatePipeline (e.g. LastWindowsUpdateSensor). Same bridge route.
    public sealed class PowerShell : IDisposable
    {
        private readonly List<Command> _cmds = new List<Command>();
        public Runspace Runspace { get; set; }
        private PowerShell() { }
        public static PowerShell Create() { return new PowerShell(); }
        public PowerShell AddScript(string script) { _cmds.Add(new Command(script, true)); return this; }
        public PowerShell AddScript(string script, bool useLocalScope) { _cmds.Add(new Command(script, true)); return this; }
        public PowerShell AddCommand(string cmdlet) { _cmds.Add(new Command(cmdlet)); return this; }
        public PowerShell AddParameter(string name, object value)
        { if (_cmds.Count > 0) _cmds[_cmds.Count - 1].Parameters.Add(new CommandParameter(name, value)); return this; }
        public PowerShell AddParameter(string name)
        { if (_cmds.Count > 0) _cmds[_cmds.Count - 1].Parameters.Add(new CommandParameter(name)); return this; }
        public PowerShell AddArgument(object value)
        { if (_cmds.Count > 0) _cmds[_cmds.Count - 1].Parameters.Add(new CommandParameter(null, value)); return this; }
        public Collection<PSObject> Invoke()
        { return Bridge.InvokePipeline(Runspace != null ? Runspace.Conn : null, _cmds); }
        public void Dispose() { }
    }

    // PSSerializer.Deserialize(clixml) → a PSObject whose ImmediateBaseObject is an ArrayList
    // of the deserialized PSObjects (the shape the PowerShell-based WU helper expects when it
    // decodes a remote response: an ArrayList in ImmediateBaseObject, with immediateBaseObjectIsEmpty set).
    public static class PSSerializer
    {
        public static object Deserialize(string source)
        {
            var rows = Bridge.Deserialize(source);
            var arr = new System.Collections.ArrayList();
            foreach (var r in rows) arr.Add(r);
            var wrapper = new PSObject();
            wrapper.ImmediateBaseObject = (arr.Count > 0) ? arr : null;
            return wrapper;
        }
        public static string Serialize(object source) { return ""; }
    }

    internal static class Bridge
    {
        internal static string Unprotect(SecureString ss)
        {
            if (ss == null) return "";
            IntPtr p = IntPtr.Zero;
            try { p = Marshal.SecureStringToGlobalAllocUnicode(ss); return Marshal.PtrToStringUni(p); }
            finally { if (p != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(p); }
        }

        private static string Endpoint(string path)
        {
            string h = Environment.GetEnvironmentVariable("PSRP_BRIDGE_ADDR"); if (string.IsNullOrEmpty(h)) h = "127.0.0.1";
            string pt = Environment.GetEnvironmentVariable("PSRP_BRIDGE_PORT"); if (string.IsNullOrEmpty(pt)) pt = "8911";
            return "http://" + h + ":" + pt + path;
        }

        // PSSerializer.Deserialize: send the CLIXML to the sidecar (/deserialize), get back
        // the same name->{t,v} rows, and materialize a List<PSObject>.
        internal static List<PSObject> Deserialize(string clixml)
        {
            var sb = new StringBuilder("{"); J(sb, "clixml", clixml); sb.Append('}');
            string resp = Post(Endpoint("/deserialize"), sb.ToString());
            var map = Json.Parse(resp) as Dictionary<string, object>;
            var outp = new List<PSObject>();
            if (map == null) return outp;
            object ok; map.TryGetValue("ok", out ok);
            if (!(ok is bool) || !(bool)ok) return outp;
            object objs; map.TryGetValue("objects", out objs);
            var list = objs as List<object>;
            if (list != null)
                foreach (var o in list)
                {
                    var row = o as Dictionary<string, object>; if (row == null) continue;
                    var pso = new PSObject();
                    foreach (var kv in row) pso.AddProperty(kv.Key, Coerce(kv.Value as Dictionary<string, object>));
                    outp.Add(pso);
                }
            return outp;
        }

        internal static Collection<PSObject> InvokePipeline(WSManConnectionInfo c, IEnumerable<Command> commands)
        {
            if (c == null)
                throw new PSRemotingTransportException(
                    "local runspace not supported under the Wine PSRP bridge — monitor a remote host");
            // shellUri "…/powershell/Microsoft.Exchange" -> configuration_name "Microsoft.Exchange"
            string cfg = c.ShellUri ?? "";
            int slash = cfg.LastIndexOf('/'); if (slash >= 0) cfg = cfg.Substring(slash + 1);
            if (cfg.Length == 0) cfg = "Microsoft.PowerShell";

            var sb = new StringBuilder();
            sb.Append('{');
            J(sb, "host", c.ComputerName); sb.Append(',');
            sb.Append("\"port\":").Append(c.Port > 0 ? c.Port : 5985).Append(',');
            sb.Append("\"ssl\":").Append(c.UseSsl ? "true" : "false").Append(',');
            J(sb, "path", c.AppName ?? ""); sb.Append(',');
            J(sb, "configuration_name", cfg); sb.Append(',');
            J(sb, "auth", "negotiate"); sb.Append(',');
            J(sb, "username", c.Credential != null ? c.Credential.UserName : ""); sb.Append(',');
            J(sb, "password", c.Credential != null ? Unprotect(c.Credential.Password) : ""); sb.Append(',');
            sb.Append("\"commands\":[");
            bool first = true;
            foreach (var cmd in commands)
            {
                if (!first) sb.Append(','); first = false;
                if (cmd.IsScript) { sb.Append("{\"kind\":\"script\","); J(sb, "text", cmd.CommandText); sb.Append('}'); }
                else
                {
                    sb.Append("{\"kind\":\"command\","); J(sb, "name", cmd.CommandText); sb.Append(",\"parameters\":[");
                    bool pf = true;
                    foreach (var p in cmd.Parameters)
                    {
                        if (!pf) sb.Append(','); pf = false;
                        sb.Append('{'); J(sb, "name", p.Name); sb.Append(',');
                        sb.Append("\"value\":"); JVal(sb, p.Value); sb.Append('}');
                    }
                    sb.Append("]}");
                }
            }
            sb.Append("]}");

            string resp = Post(Endpoint("/psrp"), sb.ToString());
            object root = Json.Parse(resp);
            var map = root as Dictionary<string, object>;
            if (map == null) throw new RuntimeException("psrp-sidecar: bad response");
            object ok; map.TryGetValue("ok", out ok);
            if (!(ok is bool) || !(bool)ok)
            {
                object err; map.TryGetValue("error", out err);
                throw new PSRemotingTransportException(err != null ? err.ToString() : "psrp-sidecar error");
            }
            var result = new Collection<PSObject>();
            object objs; map.TryGetValue("objects", out objs);
            var list = objs as List<object>;
            if (list != null)
            {
                foreach (var o in list)
                {
                    var row = o as Dictionary<string, object>;
                    if (row == null) continue;
                    var pso = new PSObject();
                    object scalar;
                    if (row.Count == 1 && row.TryGetValue("__value__", out scalar))
                    {
                        // a primitive pipeline result — expose via BaseObject/ToString
                        pso.ImmediateBaseObject = Coerce(scalar as Dictionary<string, object>);
                    }
                    else
                    {
                        foreach (var kv in row)
                            pso.AddProperty(kv.Key, Coerce(kv.Value as Dictionary<string, object>));
                    }
                    result.Add(pso);
                }
            }
            return result;
        }

        private static string Post(string url, string body)
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "POST";
            req.ContentType = "application/json";
            req.Timeout = 300000;
            // shared-secret gate: the sidecar requires this when BRIDGE_TOKEN is set.
            string tok = Environment.GetEnvironmentVariable("PSRP_BRIDGE_TOKEN");
            if (!string.IsNullOrEmpty(tok)) req.Headers.Add("X-Bridge-Token", tok);
            byte[] data = Encoding.UTF8.GetBytes(body);
            req.ContentLength = data.Length;
            using (var s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var rd = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return rd.ReadToEnd();
        }

        // Materialize a wire cell {t,v} to a CLR value, honoring the type tag so the
        // helpers' casts work: "I"->int (or long if out of range), "D"->double, "B"->bool,
        // "N"->null, else string. (JSON parses all integers as long, so without this an
        // (int)Value cast in a helper would throw InvalidCastException.)
        private static object Coerce(Dictionary<string, object> cell)
        {
            if (cell == null) return null;
            object t, v; cell.TryGetValue("t", out t); cell.TryGetValue("v", out v);
            if (v == null) return null;
            string ts = t as string;
            switch (ts)
            {
                case "I":
                    long l = Convert.ToInt64(v);
                    return (l >= int.MinValue && l <= int.MaxValue) ? (object)(int)l : (object)l;
                case "D": return Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture);
                case "B": return Convert.ToBoolean(v);
                case "N": return null;
                default: return v.ToString();
            }
        }

        private static void J(StringBuilder sb, string k, string v)
        { sb.Append('"').Append(k).Append("\":"); JStr(sb, v); }
        private static void JVal(StringBuilder sb, object v)
        {
            if (v == null) { sb.Append("null"); return; }
            if (v is bool) { sb.Append((bool)v ? "true" : "false"); return; }
            if (v is int || v is long || v is double || v is float) { sb.Append(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)); return; }
            JStr(sb, v.ToString());
        }
        private static void JStr(StringBuilder sb, string v)
        {
            if (v == null) { sb.Append("null"); return; }
            sb.Append('"');
            foreach (char ch in v)
            {
                switch (ch)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                        else sb.Append(ch);
                        break;
                }
            }
            sb.Append('"');
        }
    }

    // Minimal JSON parser (object/array/string/number/bool/null) — dependency-free.
    internal static class Json
    {
        public static object Parse(string s) { int i = 0; var v = ParseValue(s, ref i); return v; }
        private static void Ws(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }
        private static object ParseValue(string s, ref int i)
        {
            Ws(s, ref i);
            char c = s[i];
            if (c == '{') return ParseObj(s, ref i);
            if (c == '[') return ParseArr(s, ref i);
            if (c == '"') return ParseStr(s, ref i);
            if (c == 't') { i += 4; return true; }
            if (c == 'f') { i += 5; return false; }
            if (c == 'n') { i += 4; return null; }
            return ParseNum(s, ref i);
        }
        private static Dictionary<string, object> ParseObj(string s, ref int i)
        {
            var d = new Dictionary<string, object>(); i++; Ws(s, ref i);
            if (s[i] == '}') { i++; return d; }
            while (true)
            {
                Ws(s, ref i); string k = ParseStr(s, ref i); Ws(s, ref i);
                i++; // ':'
                object v = ParseValue(s, ref i); d[k] = v; Ws(s, ref i);
                if (s[i] == ',') { i++; continue; }
                i++; break; // '}'
            }
            return d;
        }
        private static List<object> ParseArr(string s, ref int i)
        {
            var l = new List<object>(); i++; Ws(s, ref i);
            if (s[i] == ']') { i++; return l; }
            while (true)
            {
                object v = ParseValue(s, ref i); l.Add(v); Ws(s, ref i);
                if (s[i] == ',') { i++; continue; }
                i++; break; // ']'
            }
            return l;
        }
        private static string ParseStr(string s, ref int i)
        {
            var sb = new StringBuilder(); i++; // opening "
            while (s[i] != '"')
            {
                char c = s[i++];
                if (c == '\\')
                {
                    char e = s[i++];
                    switch (e)
                    {
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'b': sb.Append('\b'); break;
                        case 'f': sb.Append('\f'); break;
                        case '/': sb.Append('/'); break;
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case 'u': sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16)); i += 4; break;
                        default: sb.Append(e); break;
                    }
                }
                else sb.Append(c);
            }
            i++; // closing "
            return sb.ToString();
        }
        private static object ParseNum(string s, ref int i)
        {
            int st = i;
            while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '-' || s[i] == '+' || s[i] == '.' || s[i] == 'e' || s[i] == 'E')) i++;
            string num = s.Substring(st, i - st);
            long lv;
            if (long.TryParse(num, out lv)) return lv;
            return double.Parse(num, System.Globalization.CultureInfo.InvariantCulture);
        }
    }
}
