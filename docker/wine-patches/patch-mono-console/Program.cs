// patch-mono-console — make Wine-Mono's System.Console.CursorVisible getter THROW on
// redirected output, matching .NET Framework. (Mono returns false instead of throwing;
// .NET Framework throws IOException when stdout isn't a console.) PRTG's .NET console
// helpers use `try { _ = Console.CursorVisible } catch { headless = true }` to detect
// headless operation; under Mono the getter never throws, so the helper takes the
// interactive WinForms path and hangs. This patches Mono's OWN mscorlib (open-source,
// MIT) — never a Paessler binary — same gap-fill philosophy as System.Management.patched.dll.
//
// Prepends to System.Console::get_CursorVisible:
//     if (Console.IsOutputRedirected) throw new IOException("The handle is invalid.");
//
// Usage: patch-mono-console <mscorlib-in.dll> <mscorlib-out.dll>
using System;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

class P {
    static int Main(string[] a) {
        if (a.Length < 2) { Console.Error.WriteLine("usage: <in> <out>"); return 64; }
        var asm = AssemblyDefinition.ReadAssembly(a[0], new ReaderParameters { ReadWrite = false });
        var m = asm.MainModule;
        var console = m.GetType("System.Console");
        if (console == null) { Console.Error.WriteLine("System.Console not found"); return 2; }
        var getCV = console.Methods.First(x => x.Name == "get_CursorVisible" && x.IsStatic);
        var getRedir = console.Methods.First(x => x.Name == "get_IsOutputRedirected" && x.IsStatic);
        var ioe = m.GetType("System.IO.IOException");
        var ioeCtor = ioe.Methods.First(x => x.IsConstructor && x.Parameters.Count == 1
            && x.Parameters[0].ParameterType.Name == "String");

        // idempotency guard: skip if already patched (first instr already calls get_IsOutputRedirected)
        var ins = getCV.Body.Instructions;
        if (ins.Count > 0 && ins[0].OpCode == OpCodes.Call && ins[0].Operand == getRedir) {
            asm.Write(a[1]); Console.WriteLine("already patched — copied through"); return 0;
        }
        var il = getCV.Body.GetILProcessor();
        var first = ins[0];
        // throw branch (inserted before `first`)
        var newobj = Instruction.Create(OpCodes.Newobj, getCV.Module.ImportReference(ioeCtor));
        il.InsertBefore(first, Instruction.Create(OpCodes.Call, getRedir));
        il.InsertBefore(first, Instruction.Create(OpCodes.Brfalse, first));
        il.InsertBefore(first, Instruction.Create(OpCodes.Ldstr, "The handle is invalid."));
        il.InsertBefore(first, newobj);
        il.InsertBefore(first, Instruction.Create(OpCodes.Throw));
        asm.Write(a[1]);
        Console.WriteLine("patched System.Console::get_CursorVisible (throw on IsOutputRedirected)");
        return 0;
    }
}
