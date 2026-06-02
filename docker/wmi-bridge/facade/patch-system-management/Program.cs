using System;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;
class P {
  static int Main(string[] a){
    var asm = AssemblyDefinition.ReadAssembly(a[0], new ReaderParameters{ReadWrite=false});
    int n=0;
    foreach(var t in asm.MainModule.GetTypes()){
      foreach(var m in t.Methods.Where(m=>m.Name=="AuthNotSup" && m.HasBody)){
        var il=m.Body.GetILProcessor();
        m.Body.Instructions.Clear();
        m.Body.ExceptionHandlers.Clear();
        m.Body.Variables.Clear();
        il.Append(il.Create(OpCodes.Ret));
        Console.WriteLine("patched "+t.FullName+"::"+m.Name);
        n++;
      }
    }
    if(n==0){ Console.WriteLine("AuthNotSup NOT FOUND"); return 2; }
    asm.Write(a[1]);
    Console.WriteLine("wrote "+a[1]+" ("+n+" method(s))");
    return 0;
  }
}
