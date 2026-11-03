using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class StudioLauncher {
    [STAThread] static int Main(string[] args) {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string script = Path.Combine(root, "bootstrap.ps1");
            if (!File.Exists(script)) throw new Exception("Extract the entire UAV Studio ZIP before starting.");
            var start = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (var p = Process.Start(start)) { p.WaitForExit(); return p.ExitCode; }
        } catch (Exception e) {
            MessageBox.Show(e.Message, "UAV Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
