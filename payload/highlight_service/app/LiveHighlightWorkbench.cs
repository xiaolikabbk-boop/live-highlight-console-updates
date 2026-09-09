using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class LiveHighlightWorkbench
{
    internal const string Address = "http://127.0.0.1:8876/";

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        bool background = args.Length > 0 && string.Equals(args[0], "background", StringComparison.OrdinalIgnoreCase);
        string appDirectory = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string serviceDirectory = Directory.GetParent(appDirectory).FullName;
        string rootDirectory = Directory.GetParent(serviceDirectory).FullName;

        if (args.Length > 0 && string.Equals(args[0], "selftest", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                string runtimeVersion = CoreWebView2Environment.GetAvailableBrowserVersionString();
                if (string.IsNullOrWhiteSpace(runtimeVersion)) return 2;
                using (WebView2 probe = new WebView2()) { }
                TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
                listener.Start();
                int testPort = ((IPEndPoint)listener.LocalEndpoint).Port;
                bool socketReady = IsPortReady(testPort);
                listener.Stop();
                if (!socketReady) return 4;
                return 0;
            }
            catch { return 3; }
        }

        if (background)
        {
            try { StartBackend(rootDirectory, false); return 0; }
            catch (Exception exception) { WriteLauncherError(rootDirectory, exception); return 1; }
        }

        Application.Run(new WorkbenchForm(rootDirectory));
        return 0;
    }

    internal static string GetSelectedLegacyRoot(string rootDirectory)
    {
        string selectionFile = Path.Combine(rootDirectory, "desktop-data-source.txt");
        if (!File.Exists(selectionFile)) return null;
        string selected = File.ReadAllText(selectionFile, Encoding.UTF8).Trim();
        return IsLegacyRoot(selected) ? Path.GetFullPath(selected) : null;
    }

    internal static bool IsLegacyRoot(string path)
    {
        if (string.IsNullOrWhiteSpace(path)) return false;
        try { return File.Exists(Path.Combine(Path.GetFullPath(path), "highlight_service", "data", "highlight.db")); }
        catch { return false; }
    }

    internal static Process StartBackend(string rootDirectory, bool captureOutput)
    {
        string progressLog = Path.Combine(rootDirectory, "startup-progress.log");
        File.WriteAllText(progressLog,
            DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " desktop workbench started" + Environment.NewLine,
            new UTF8Encoding(true));
        string startScript = Path.Combine(rootDirectory, "start_console.ps1");
        if (!File.Exists(startScript)) throw new FileNotFoundException("start_console.ps1 is missing.", startScript);
        string windowsPowerShell = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
        if (!File.Exists(windowsPowerShell)) windowsPowerShell = "powershell.exe";
        ProcessStartInfo info = new ProcessStartInfo();
        info.FileName = windowsPowerShell;
        info.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + startScript + "\" -NoDesktopWindow";
        info.WorkingDirectory = rootDirectory;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WindowStyle = ProcessWindowStyle.Hidden;
        info.RedirectStandardOutput = captureOutput;
        info.RedirectStandardError = captureOutput;
        string legacyRoot = GetSelectedLegacyRoot(rootDirectory);
        if (!string.IsNullOrEmpty(legacyRoot) &&
            !string.Equals(Path.GetFullPath(legacyRoot).TrimEnd('\\'), Path.GetFullPath(rootDirectory).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
        {
            info.EnvironmentVariables["HIGHLIGHT_DATA_DIR"] = Path.Combine(legacyRoot, "highlight_service", "data");
            info.EnvironmentVariables["HIGHLIGHT_RECORDER_ROOT"] = Path.Combine(legacyRoot, "DouyinLiveRecorder_v4.0.7");
        }
        Process process = Process.Start(info);
        if (process == null) throw new InvalidOperationException("Windows could not create the background process.");
        if (captureOutput)
        {
            string backendLog = Path.Combine(rootDirectory, "backend-startup.log");
            try { File.WriteAllText(backendLog, "", new UTF8Encoding(true)); } catch { }
            DataReceivedEventHandler writer = delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (eventArgs.Data == null) return;
                try { File.AppendAllText(backendLog, eventArgs.Data + Environment.NewLine, new UTF8Encoding(true)); } catch { }
            };
            process.OutputDataReceived += writer;
            process.ErrorDataReceived += writer;
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        return process;
    }

    internal static bool IsReady()
    {
        return IsPortReady(8876);
    }

    internal static bool IsPortReady(int port)
    {
        TcpClient client = new TcpClient();
        try
        {
            IAsyncResult pending = client.BeginConnect(IPAddress.Loopback, port, null, null);
            if (!pending.AsyncWaitHandle.WaitOne(1000)) return false;
            client.EndConnect(pending);
            return client.Connected;
        }
        catch { return false; }
        finally { client.Close(); }
    }

    internal static string WriteLauncherError(string rootDirectory, Exception exception)
    {
        string path = Path.Combine(rootDirectory, "launcher-error.log");
        try
        {
            File.WriteAllText(path, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + Environment.NewLine + exception,
                new UTF8Encoding(true));
        }
        catch { }
        return path;
    }
}

internal sealed class WorkbenchForm : Form
{
    private readonly string rootDirectory;
    private readonly Label statusLabel;
    private readonly ProgressBar progressBar;
    private readonly Timer readyTimer;
    private Process backendProcess;
    private WebView2 browser;
    private Label navigationStatus;
    private int attempts;

    internal WorkbenchForm(string root)
    {
        rootDirectory = root;
        Text = "直播高光工作台";
        StartPosition = FormStartPosition.CenterScreen;
        WindowState = FormWindowState.Maximized;
        MinimumSize = new Size(1000, 680);
        BackColor = Color.FromArgb(244, 240, 255);
        KeyPreview = true;
        KeyDown += HandleRefreshShortcut;

        Panel splash = new Panel();
        splash.Name = "startupSplash";
        splash.Dock = DockStyle.Fill;
        splash.BackColor = Color.FromArgb(245, 241, 255);
        Controls.Add(splash);

        Label title = new Label();
        title.Text = "直播高光工作台";
        title.Font = new Font("Microsoft YaHei UI", 25F, FontStyle.Bold);
        title.ForeColor = Color.FromArgb(88, 45, 180);
        title.AutoSize = true;
        title.Location = new Point(72, 70);
        splash.Controls.Add(title);

        statusLabel = new Label();
        statusLabel.Text = "正在启动本地处理引擎，请稍候…";
        statusLabel.Font = new Font("Microsoft YaHei UI", 12F);
        statusLabel.ForeColor = Color.FromArgb(87, 74, 118);
        statusLabel.AutoSize = true;
        statusLabel.Location = new Point(76, 132);
        splash.Controls.Add(statusLabel);

        progressBar = new ProgressBar();
        progressBar.Style = ProgressBarStyle.Marquee;
        progressBar.MarqueeAnimationSpeed = 28;
        progressBar.Size = new Size(470, 8);
        progressBar.Location = new Point(80, 180);
        splash.Controls.Add(progressBar);

        readyTimer = new Timer();
        readyTimer.Interval = 1000;
        readyTimer.Tick += CheckReady;
        Shown += StartWorkbench;
    }

    private void StartWorkbench(object sender, EventArgs e)
    {
        try
        {
            if (!LiveHighlightWorkbench.IsReady())
                backendProcess = LiveHighlightWorkbench.StartBackend(rootDirectory, true);
            readyTimer.Start();
            CheckReady(null, EventArgs.Empty);
        }
        catch (Exception exception) { ShowFailure(exception); }
    }

    private async void CheckReady(object sender, EventArgs e)
    {
        attempts++;
        if (LiveHighlightWorkbench.IsReady())
        {
            readyTimer.Stop();
            await ShowEmbeddedWorkbench();
            return;
        }
        if (backendProcess != null && backendProcess.HasExited)
        {
            readyTimer.Stop();
            string serviceLog = Path.Combine(rootDirectory, "startup-error.log");
            string backendLog = Path.Combine(rootDirectory, "backend-startup.log");
            string detail = File.Exists(serviceLog)
                ? File.ReadAllText(serviceLog)
                : (File.Exists(backendLog) && new FileInfo(backendLog).Length > 0
                    ? File.ReadAllText(backendLog)
                    : "后台处理引擎提前退出，代码 " + backendProcess.ExitCode + "。");
            ShowFailure(new InvalidOperationException(detail));
            return;
        }
        if (attempts >= 180)
        {
            readyTimer.Stop();
            ShowFailure(new TimeoutException("本地处理引擎在 3 分钟内没有完成启动。"));
            return;
        }
        statusLabel.Text = attempts < 15
            ? "正在启动本地处理引擎，请稍候…"
            : "首次启动或积压任务较多时可能需要更久，请不要重复点击。";
    }

    private async Task ShowEmbeddedWorkbench()
    {
        try
        {
            browser = new WebView2();
            browser.Dock = DockStyle.Fill;
            string legacyRoot = LiveHighlightWorkbench.GetSelectedLegacyRoot(rootDirectory);
            string activeRoot = string.IsNullOrEmpty(legacyRoot) ? rootDirectory : legacyRoot;
            string profile = Path.Combine(activeRoot, "highlight_service", "data", "desktop_profile");
            CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, profile);
            await browser.EnsureCoreWebView2Async(environment);
            browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
            browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
            browser.CoreWebView2.NavigationStarting += delegate { BeginInvoke((Action)(() => navigationStatus.Text = "正在加载…")); };
            browser.CoreWebView2.NavigationCompleted += delegate { BeginInvoke((Action)(() => navigationStatus.Text = "每30秒自动更新")); };

            TableLayoutPanel layout = new TableLayoutPanel();
            layout.Dock = DockStyle.Fill;
            layout.RowCount = 2;
            layout.ColumnCount = 1;
            layout.Margin = Padding.Empty;
            layout.Padding = Padding.Empty;
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42F));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            layout.Controls.Add(CreateDesktopToolbar(), 0, 0);
            layout.Controls.Add(browser, 0, 1);

            browser.Source = new Uri(LiveHighlightWorkbench.Address);
            Controls.Clear();
            Controls.Add(layout);
            browser.Focus();
        }
        catch (Exception exception)
        {
            ShowFailure(new InvalidOperationException("桌面界面组件启动失败。请确认 Microsoft Edge WebView2 Runtime 已安装。", exception));
        }
    }

    private Panel CreateDesktopToolbar()
    {
        Panel toolbar = new Panel();
        toolbar.Dock = DockStyle.Top;
        toolbar.Height = 42;
        toolbar.BackColor = Color.FromArgb(79, 42, 166);

        Label desktopTitle = new Label();
        desktopTitle.Text = "直播高光工作台 · 桌面版";
        desktopTitle.ForeColor = Color.White;
        desktopTitle.Font = new Font("Microsoft YaHei UI", 10.5F, FontStyle.Bold);
        desktopTitle.AutoSize = true;
        desktopTitle.Location = new Point(16, 11);
        toolbar.Controls.Add(desktopTitle);

        navigationStatus = new Label();
        navigationStatus.Text = "每30秒自动更新";
        navigationStatus.ForeColor = Color.FromArgb(220, 210, 248);
        navigationStatus.Font = new Font("Microsoft YaHei UI", 9F);
        navigationStatus.AutoSize = true;
        navigationStatus.Location = new Point(210, 12);
        toolbar.Controls.Add(navigationStatus);

        FlowLayoutPanel actions = new FlowLayoutPanel();
        actions.Dock = DockStyle.Right;
        actions.Width = 226;
        actions.FlowDirection = FlowDirection.LeftToRight;
        actions.WrapContents = false;
        actions.Padding = new Padding(0, 5, 8, 0);
        actions.BackColor = Color.Transparent;

        Button refresh = new Button();
        refresh.Text = "刷新";
        refresh.Size = new Size(82, 29);
        refresh.FlatStyle = FlatStyle.Flat;
        refresh.FlatAppearance.BorderColor = Color.FromArgb(190, 170, 240);
        refresh.ForeColor = Color.White;
        refresh.BackColor = Color.FromArgb(105, 67, 194);
        refresh.Click += delegate { ReloadWorkbench(); };
        actions.Controls.Add(refresh);

        Button migrate = new Button();
        migrate.Text = "接入旧版数据";
        migrate.Size = new Size(120, 29);
        migrate.FlatStyle = FlatStyle.Flat;
        migrate.FlatAppearance.BorderColor = Color.FromArgb(190, 170, 240);
        migrate.ForeColor = Color.White;
        migrate.BackColor = Color.FromArgb(105, 67, 194);
        migrate.Click += ChooseLegacyData;
        actions.Controls.Add(migrate);
        toolbar.Controls.Add(actions);
        return toolbar;
    }

    private void ReloadWorkbench()
    {
        if (browser == null || browser.CoreWebView2 == null) return;
        navigationStatus.Text = "正在刷新…";
        browser.Reload();
    }

    private void HandleRefreshShortcut(object sender, KeyEventArgs e)
    {
        if (e.KeyCode != Keys.F5 && !(e.Control && e.KeyCode == Keys.R)) return;
        e.Handled = true;
        e.SuppressKeyPress = true;
        ReloadWorkbench();
    }

    private async void ChooseLegacyData(object sender, EventArgs e)
    {
        using (FolderBrowserDialog dialog = new FolderBrowserDialog())
        {
            dialog.Description = "请选择旧版直播监控程序的最外层文件夹";
            dialog.ShowNewFolderButton = false;
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            string selected = dialog.SelectedPath;
            if (string.Equals(Path.GetFileName(selected), "highlight_service", StringComparison.OrdinalIgnoreCase))
                selected = Directory.GetParent(selected).FullName;
            if (!LiveHighlightWorkbench.IsLegacyRoot(selected))
            {
                MessageBox.Show("这个目录中没有找到旧版数据库：\r\nhighlight_service\\data\\highlight.db\r\n\r\n请重新选择旧程序最外层目录。",
                    "没有识别到旧版数据", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            if (string.Equals(Path.GetFullPath(selected).TrimEnd('\\'), Path.GetFullPath(rootDirectory).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            {
                MessageBox.Show("当前桌面程序已经直接使用这个目录的数据，无需重复导入。",
                    "数据已经接入", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            DialogResult confirm = MessageBox.Show(
                "将接入以下旧版目录：\r\n" + selected +
                "\r\n\r\n直播间、数据库、录像、转写队列和成片会继续保留在原位置，桌面版只建立安全连接，不移动、不删除、不覆盖。密钥仍在桌面版中重新填写。\r\n\r\n确认后工作台将重启。",
                "接入旧版数据", MessageBoxButtons.OKCancel, MessageBoxIcon.Information);
            if (confirm != DialogResult.OK) return;
            File.WriteAllText(Path.Combine(rootDirectory, "desktop-data-source.txt"), selected, new UTF8Encoding(true));
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(LiveHighlightWorkbench.Address + "api/system/shutdown");
                request.Method = "POST";
                request.ContentLength = 0;
                request.Timeout = 4000;
                request.Proxy = null;
                using (request.GetResponse()) { }
            }
            catch { }
            await Task.Delay(2500);
            Process.Start(new ProcessStartInfo(Application.ExecutablePath) { UseShellExecute = true });
            Close();
        }
    }

    private void ShowFailure(Exception exception)
    {
        progressBar.Style = ProgressBarStyle.Blocks;
        progressBar.Value = 0;
        statusLabel.Text = "启动失败，错误详情已经保存。";
        string log = LiveHighlightWorkbench.WriteLauncherError(rootDirectory, exception);
        MessageBox.Show("直播高光工作台启动失败。\r\n\r\n" + exception.Message +
            "\r\n\r\n请把这个文件发回诊断：\r\n" + log,
            "直播高光工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
