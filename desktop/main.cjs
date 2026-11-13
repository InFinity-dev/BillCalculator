"use strict";

const { app, BrowserWindow, dialog, session, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

let mainWindow = null;
let backendProcess = null;
let backendUrl = null;
let backendLog = null;
let isStopping = false;
let quitAfterStop = false;
const shutdownToken = crypto.randomBytes(32).toString("hex");

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
}

function secureWebPreferences() {
  return {
    nodeIntegration: false,
    nodeIntegrationInWorker: false,
    contextIsolation: true,
    sandbox: true,
    webSecurity: true,
    allowRunningInsecureContent: false,
    webviewTag: false,
  };
}

function loopbackUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname);
  } catch (_error) {
    return false;
  }
}

function externalUrl(value) {
  try {
    return ["https:", "http:"].includes(new URL(value).protocol);
  } catch (_error) {
    return false;
  }
}

function protectContents(contents) {
  contents.on("will-navigate", (event, destination) => {
    if (loopbackUrl(destination)) return;
    event.preventDefault();
    if (externalUrl(destination)) void shell.openExternal(destination);
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    if (loopbackUrl(url)) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 1120,
          height: 820,
          minWidth: 760,
          minHeight: 600,
          autoHideMenuBar: process.platform !== "darwin",
          webPreferences: secureWebPreferences(),
        },
      };
    }
    if (externalUrl(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("did-create-window", (child) => protectContents(child.webContents));
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 920,
    minHeight: 680,
    show: false,
    backgroundColor: "#f5f7fa",
    title: "BillCalculator",
    autoHideMenuBar: process.platform !== "darwin",
    webPreferences: secureWebPreferences(),
  });
  protectContents(window.webContents);
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  void window.loadFile(path.join(__dirname, "loading.html"));
  return window;
}

function availablePort() {
  return new Promise((resolve, reject) => {
    const listener = net.createServer();
    listener.unref();
    listener.on("error", reject);
    listener.listen(0, "127.0.0.1", () => {
      const address = listener.address();
      listener.close(() => resolve(address.port));
    });
  });
}

function developmentPython() {
  if (process.env.BILLCALC_PYTHON) return process.env.BILLCALC_PYTHON;
  const candidate = process.platform === "win32"
    ? path.join(app.getAppPath(), ".venv", "Scripts", "python.exe")
    : path.join(app.getAppPath(), ".venv", "bin", "python");
  if (fs.existsSync(candidate)) return candidate;
  return process.platform === "win32" ? "python" : "python3";
}

function backendCommand(port) {
  const argumentsList = ["serve", "--host", "127.0.0.1", "--port", String(port)];
  if (app.isPackaged) {
    const executable = process.platform === "win32"
      ? "billcalculator-backend.exe"
      : "billcalculator-backend";
    return {
      executable: path.join(process.resourcesPath, "backend", executable),
      argumentsList,
      cwd: process.resourcesPath,
    };
  }
  return {
    executable: developmentPython(),
    argumentsList: [path.join(app.getAppPath(), "desktop_backend.py"), ...argumentsList],
    cwd: app.getAppPath(),
  };
}

async function startBackend() {
  const port = await availablePort();
  const userData = app.getPath("userData");
  const dataDirectory = path.join(userData, "data");
  const logDirectory = path.join(userData, "logs");
  fs.mkdirSync(dataDirectory, { recursive: true });
  fs.mkdirSync(logDirectory, { recursive: true });
  backendLog = fs.createWriteStream(path.join(logDirectory, "backend.log"), { flags: "a" });
  backendLog.write(`\n[${new Date().toISOString()}] starting BillCalculator ${app.getVersion()}\n`);

  const command = backendCommand(port);
  if (app.isPackaged && !fs.existsSync(command.executable)) {
    throw new Error(`백엔드 실행 파일이 없습니다: ${command.executable}`);
  }
  const environment = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    BILLCALC_DB_PATH: path.join(dataDirectory, "bill_calculator.db"),
    BILLCALC_ONBOARDING_STATE_PATH: path.join(dataDirectory, "app_state.json"),
    BILLCALC_TUTORIAL_DATA_DIR: path.join(dataDirectory, "tutorial_sessions"),
    BILLCALC_DESKTOP_MODE: "1",
    BILLCALC_DESKTOP_SHUTDOWN_TOKEN: shutdownToken,
    BILLCALC_SECRET_KEY: crypto.randomBytes(32).toString("hex"),
    BILLCALC_APP_VERSION: app.getVersion(),
    BILLCALC_DEBUG: "0",
  };
  delete environment.BILLCALC_DATABASE_URI;

  backendProcess = spawn(command.executable, command.argumentsList, {
    cwd: command.cwd,
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  return new Promise((resolve, reject) => {
    let settled = false;
    let outputBuffer = "";
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("로컬 백엔드가 제한 시간 안에 시작되지 않았습니다."));
    }, 60000);

    const consume = (chunk) => {
      const text = chunk.toString("utf8");
      backendLog?.write(text);
      outputBuffer += text;
      const lines = outputBuffer.split(/\r?\n/);
      outputBuffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("BILLCALC_READY=")) continue;
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        backendUrl = line.slice("BILLCALC_READY=".length).trim();
        resolve(backendUrl);
      }
    };
    backendProcess.stdout.on("data", consume);
    backendProcess.stderr.on("data", consume);
    backendProcess.once("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      reject(error);
    });
    backendProcess.once("exit", (code) => {
      backendLog?.write(`\nbackend exited with code ${code}\n`);
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`로컬 백엔드가 시작 중 종료되었습니다. (code ${code})`));
        return;
      }
      backendProcess = null;
      if (!isStopping && mainWindow && !mainWindow.isDestroyed()) {
        void showStartupError("로컬 백엔드가 예기치 않게 종료되었습니다. 앱을 다시 실행해주세요.");
      }
    });
  });
}

function requestShutdown() {
  if (!backendUrl) return Promise.resolve();
  return new Promise((resolve) => {
    const parsed = new URL("/__desktop__/shutdown", backendUrl);
    const request = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port,
        path: parsed.pathname,
        method: "POST",
        headers: { "X-BillCalculator-Shutdown": shutdownToken, "Content-Length": "0" },
        timeout: 2500,
      },
      (response) => {
        response.resume();
        response.on("end", resolve);
      },
    );
    request.on("timeout", () => {
      request.destroy();
      resolve();
    });
    request.on("error", resolve);
    request.end();
  });
}

function waitForExit(timeoutMs) {
  if (!backendProcess) return Promise.resolve();
  return new Promise((resolve) => {
    const processReference = backendProcess;
    const timeout = setTimeout(resolve, timeoutMs);
    processReference.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function stopBackend() {
  if (isStopping) return;
  isStopping = true;
  await requestShutdown();
  await waitForExit(5000);
  if (backendProcess) {
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/pid", String(backendProcess.pid), "/T", "/F"], { windowsHide: true });
    } else {
      backendProcess.kill("SIGTERM");
      await waitForExit(2000);
      if (backendProcess) backendProcess.kill("SIGKILL");
    }
  }
  backendProcess = null;
  backendUrl = null;
  backendLog?.end();
  backendLog = null;
}

async function showStartupError(message) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  await mainWindow.loadFile(path.join(__dirname, "loading.html"), {
    query: { error: message, log: path.join(app.getPath("userData"), "logs", "backend.log") },
  });
}

async function launch() {
  mainWindow = createWindow();
  try {
    const url = await startBackend();
    await mainWindow.loadURL(url);
  } catch (error) {
    backendLog?.write(`\nstartup error: ${error.stack || error.message}\n`);
    await showStartupError(error.message || String(error));
    dialog.showErrorBox("BillCalculator를 시작하지 못했습니다", error.message || String(error));
  }
}

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

app.on("before-quit", (event) => {
  if (quitAfterStop || !backendProcess) return;
  event.preventDefault();
  void stopBackend().finally(() => {
    quitAfterStop = true;
    app.quit();
  });
});

app.on("window-all-closed", () => app.quit());

if (hasSingleInstanceLock) {
  app.whenReady().then(async () => {
    session.defaultSession.setPermissionCheckHandler(() => false);
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    await launch();
  });
}
