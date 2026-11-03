// Preserve the application's stdin/stdout protocol while running conda-forge ExifTool.
using System;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
static class ExifToolShim {
    static string Quote(string s) {
        return "\"" + Regex.Replace(Regex.Replace(s, "(\\\\*)\"", "$1$1\\\""), "(\\\\+)$", "$1$1") + "\"";
    }
    static int Main(string[] args) {
        string bin = AppDomain.CurrentDomain.BaseDirectory;
        string prefix = Directory.GetParent(bin.TrimEnd(Path.DirectorySeparatorChar)).FullName;
        var p = new ProcessStartInfo(Path.Combine(prefix, "Library", "bin", "perl.exe"));
        p.Arguments = Quote(Path.Combine(bin,"exiftool"));
        foreach (var arg in args) p.Arguments += " " + Quote(arg);
        p.UseShellExecute = false; p.CreateNoWindow = true;
        p.RedirectStandardInput = true; p.RedirectStandardOutput = true; p.RedirectStandardError = true;
        using (var process = Process.Start(p)) {
            var output = process.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
            var error = process.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError());
            Task.Run(() => { try {
                var input=Console.OpenStandardInput(); var buffer=new byte[4096]; int read;
                while ((read=input.Read(buffer,0,buffer.Length))>0) {
                    process.StandardInput.BaseStream.Write(buffer,0,read);
                    process.StandardInput.BaseStream.Flush();
                }
                process.StandardInput.Close();
            } catch (IOException) {} });
            process.WaitForExit(); Task.WaitAll(output,error); return process.ExitCode;
        }
    }
}
