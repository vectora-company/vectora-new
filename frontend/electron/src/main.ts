/**
 * Vectora — main process.
 *
 * Responsabilidades:
 * - Spawn do binário do backend (``vectora-core``, PyInstaller) como sidecar (VECTORA_DESKTOP=1).
 * - Transporte IPC: unix socket (Linux/macOS) / TCP loopback (Windows). A SPA
 *   carrega de ``vectora-app://app/`` e o main encaminha tudo ao backend, sem
 *   expor porta TCP no desktop (em Linux/macOS).
 * - Health-check com retry exponencial antes de carregar a janela.
 * - Tray icon com menu (Open / Restart Backend / Quit).
 * - Deep-link ``vectora://`` (signin magic-link, deep-link de workspace).
 * - IPC tipado (``contextBridge``) exposto via ``preload.ts``.
 * - Auto-update via ``electron-updater`` repassando estado para o renderer.
 * - Crash handler com diálogo "Reiniciar" e relay para Sentry quando ligado.
 *
 * Não roda lógica de negócio — é casca nativa que orquestra o backend.
 */

import { ChildProcess } from "child_process";
import {
  DEVELOPMENT_DESKTOP_NAME,
  appImageRelaunchOptions,
  registerAppImageCommand,
  registerDevelopmentDesktop,
} from "./linux-desktop.js";
import * as fs from "fs";
import {
  app,
  BrowserWindow,
  clipboard,
  desktopCapturer,
  Menu,
  screen,
  session,
  Tray,
  type WebContents,
  dialog,
  ipcMain,
  nativeImage,
  protocol,
  safeStorage,
  shell,
  WebContentsView,
} from "electron";
import { autoUpdater } from "electron-updater";
import * as http from "http";
import * as os from "os";
import * as path from "path";
import { randomInt, randomUUID } from "crypto";
import { Readable } from "stream";
import { vectoraBuildInfo } from "./build-info.generated.js";
// tree-kill ships its own types — no @types/tree-kill needed.
import treeKill = require("tree-kill");
import { parseSetCookieHeader, buildCookieHeader } from "./cookie-utils.js";
import { createOpenPathsDispatcher, pathsFromArgv } from "./open-paths.js";
import {
  buildContextMenuTemplate,
  buildPopupTemplate,
  contextMenuLabels,
  sanitizePopupItems,
} from "./context-menu.js";
import {
  getFreePort,
  backendLaunch,
  backendPath,
  backendTransportOptions,
  isInstalledTestsMode,
  isInstalledSmokeTestMode,
  ignoreBrokenPipe,
  natsBinaryPath,
  spawnBackendProcess,
  IpcPipeParser,
  pingBackendHttp,
  fetchBackendJson,
  postBackendJson,
  waitForBackendReady,
  stopBackendProcess,
  stopBackendTree,
  resolveExternalBackendConnection,
  signLaunchHandshake,
  unixSocketIsActive,
} from "./backend-lifecycle.js";
import {
  parseGraphicsCliOverrides,
  resolveLinuxGraphicsConfiguration,
} from "./linux-graphics.js";
import { createGraphicsInfoReporter } from "./graphics-info.js";
import {
  BrowserViewManager,
  clearBrowserSessionData,
  type BrowserViewKind,
  type ManagedView,
  type ViewBounds,
} from "./browser-view-manager.js";
import {
  isValidBrowserUrl,
  isValidBrowserViewKind,
  isValidProfileId,
  isValidViewBounds,
  isValidViewId,
} from "./browser-ipc-validation.js";

function browserOwnerId(event: unknown): number {
  return (event as { sender?: { id?: number } }).sender?.id ?? -1;
}

function isTrustedBrowserSender(event: unknown): boolean {
  const candidate = event as {
    sender?: unknown;
    senderFrame?: { url?: string } | null;
  };
  return (
    candidate.sender === mainWindow?.webContents &&
    (candidate.senderFrame?.url ?? "").startsWith(`${APP_SCHEME}://`)
  );
}

import {
  computeDefaultWindowSize,
  fitBoundsToWorkArea,
} from "./window-size.js";
import {
  fetchMarketplaceThemes,
  searchMarketplaceThemes,
} from "./vscode-marketplace.js";
import {
  createRotatingUpdateBackup,
  listUpdateBackups,
  restoreUpdateBackup,
} from "./update-backup.js";
import { startUpdateDownload as startUpdateDownloadAfterBackup } from "./updater-download.js";
import {
  resolveRuntimeProfile,
  runtimeHome as resolveRuntimeHome,
  runtimeUserDataName,
} from "./runtime-profile.js";
import { DockerCliExecutor } from "./docker-cli-executor.js";
import { DmrOperations } from "./dmr-operations.js";
import {
  startElectronCdpProxy,
  waitForElectronCdpTarget,
} from "./electron-cdp-proxy.js";

ignoreBrokenPipe(process.stdout);
ignoreBrokenPipe(process.stderr);

// O backend conecta ao mesmo Chromium do Electron via CDP. O endpoint fica
// restrito ao loopback e é herdado pelo processo backend supervisionado. Em
// produção o processo usa uma porta efêmera para evitar que outro processo
// local consiga assumir o CDP apenas conhecendo uma porta fixa; o backend
// recebe o valor pelo ambiente junto com o token da ponte desktop.
const electronCdpPort =
  process.env.VECTORA_ELECTRON_CDP_PORT ?? String(randomInt(10000, 60000));
process.env.VECTORA_ELECTRON_CDP_PORT = electronCdpPort;
const electronCdpProxyPort =
  process.env.VECTORA_ELECTRON_CDP_PROXY_PORT ??
  String(randomInt(60001, 65000));
const electronCdpAuthToken =
  process.env.VECTORA_ELECTRON_CDP_AUTH_TOKEN ?? randomUUID();
process.env.VECTORA_ELECTRON_CDP_PROXY_PORT = electronCdpProxyPort;
process.env.VECTORA_ELECTRON_CDP_AUTH_TOKEN = electronCdpAuthToken;
const installedTestsMode = isInstalledTestsMode(process.argv);
const installedSmokeTestMode = isInstalledSmokeTestMode(process.argv);
const installedAutomationMode = installedTestsMode || installedSmokeTestMode;
const smokeProcessStartedAt = process.hrtime.bigint();
function emitSmokeStage(stage: string): void {
  if (!installedSmokeTestMode) return;
  const elapsedMs =
    Number(process.hrtime.bigint() - smokeProcessStartedAt) / 1e6;
  console.log(`VECTORA_EVAL_STAGE:${stage}:${elapsedMs.toFixed(3)}`);
}

const graphicsCli = parseGraphicsCliOverrides(process.argv);
if (graphicsCli.disableGpu) {
  app.commandLine.appendSwitch("disable-gpu");
  console.info("[graphics] GPU desabilitada por --disable-gpu");
}
if (!installedAutomationMode) {
  app.commandLine.appendSwitch("remote-debugging-address", "127.0.0.1");
  app.commandLine.appendSwitch("remote-debugging-port", electronCdpPort);
}
const angleOverride =
  graphicsCli.angleBackend ?? process.env.VECTORA_ANGLE_BACKEND?.trim();
if (process.platform === "linux" && !graphicsCli.disableGpu) {
  const graphics = resolveLinuxGraphicsConfiguration({
    ...process.env,
    VECTORA_ANGLE_BACKEND: angleOverride,
    VECTORA_OZONE_PLATFORM:
      graphicsCli.ozonePlatform ?? process.env.VECTORA_OZONE_PLATFORM,
  });
  app.commandLine.appendSwitch("ignore-gpu-blocklist");
  if (graphicsCli.angleBackend) {
    app.commandLine.appendSwitch("use-angle", graphicsCli.angleBackend);
  } else {
    app.commandLine.appendSwitch("use-angle", "vulkan");
  }
  app.commandLine.appendSwitch(
    "enable-features",
    "Vulkan,VulkanFromANGLE,DefaultANGLEVulkan,VaapiVideoDecoder",
  );
  app.commandLine.appendSwitch("disable-features", "SkiaGraphite");
  for (const [name, value] of graphics.switches) {
    if (value === undefined) {
      app.commandLine.appendSwitch(name);
    } else {
      app.commandLine.appendSwitch(name, value);
    }
  }
  console.info("[graphics] configuração Linux do Electron", {
    switches: graphics.switches,
  });
} else if (process.platform !== "linux" && !graphicsCli.disableGpu) {
  // Sem override explícito, o Electron escolhe o backend nativo.
  if (angleOverride) {
    app.commandLine.appendSwitch("use-angle", angleOverride);
  } else if (process.platform === "win32") {
    // Usa DirectX 11 sobre DirectX 12, ambos no caminho gráfico.
    app.commandLine.appendSwitch("use-angle", "d3d11on12");
  } else if (process.platform === "darwin") {
    app.commandLine.appendSwitch("use-angle", "metal");
  }
}

const logActualGraphicsInfo = createGraphicsInfoReporter({
  enabled: !installedAutomationMode && !graphicsCli.disableGpu,
  getInfo: () => app.getGPUInfo("complete"),
  log: (reason, info) =>
    console.info("[graphics] GPU reportada pelo Chromium", { reason, info }),
  warn: (reason, error) =>
    console.warn("[graphics] não foi possível consultar a GPU", {
      reason,
      error,
    }),
});

app.on("gpu-info-update", () => {
  void logActualGraphicsInfo("gpu-info-update");
});

let electronCdpProxy: import("node:http").Server | null = null;
if (!installedAutomationMode) {
  void app.whenReady().then(async () => {
    try {
      await waitForElectronCdpTarget(Number(electronCdpPort));
      electronCdpProxy = await startElectronCdpProxy(
        Number(electronCdpPort),
        Number(electronCdpProxyPort),
        electronCdpAuthToken,
      );
    } catch (error) {
      console.error("Falha ao validar o alvo CDP do Electron", error);
    }
  });
}

const ELECTRON_RESTART_EXIT_CODE = 42;

/**
 * Reinicia o processo Electron sem duplicar a instância supervisionada.
 *
 * Quando o backend Python iniciou o Electron como sidecar, ele é o
 * supervisor do ciclo de vida. Nesse modo, `app.relaunch()` criaria uma
 * segunda instância antes de o watcher do backend iniciar a substituta; a
 * nova instância poderia tentar criar a janela sem a conexão IPC pronta.
 * Apenas o código de saída sinaliza o watcher nesse caso. Em produção,
 * onde o Electron possui o backend, a relaunch nativa continua necessária.
 */
function requestElectronRestart(): void {
  if (process.env.VECTORA_EXTERNAL_BACKEND === "1") {
    app.exit(ELECTRON_RESTART_EXIT_CODE);
    return;
  }
  const relaunchOptions =
    process.platform === "linux" && app.isPackaged
      ? appImageRelaunchOptions(process.env.APPIMAGE)
      : undefined;
  if (relaunchOptions) app.relaunch(relaunchOptions);
  else app.relaunch();
  app.exit(ELECTRON_RESTART_EXIT_CODE);
}

interface UpdateStatus {
  state:
    | "checking"
    | "available"
    | "downloading"
    | "downloaded"
    | "error"
    | "not-available";
  message?: string;
  progress?: number;
  changelog?: string;
}

// O `name` do package.json é "vectora-desktop" (identificador do pacote npm,
// distinto de outros subprojetos do monorepo) — sem isto, Electron usa esse
// valor pra derivar o userData path (%APPDATA%\vectora-desktop\ no Windows),
// que não é o nome do produto. setName() força %APPDATA%\vectora\ sem
// precisar renomear o pacote. Chamado antes de qualquer path ser resolvido.
app.setName("vectora");
if (process.platform === "linux") {
  app.setDesktopName(
    app.isPackaged ? "vectora.desktop" : DEVELOPMENT_DESKTOP_NAME,
  );
  if (app.isPackaged && process.env.APPIMAGE) {
    try {
      registerAppImageCommand({
        home: os.homedir(),
        appImage: process.env.APPIMAGE,
      });
      console.info(
        "[appimage] Registered the terminal command at ~/.local/bin/vectora",
      );
    } catch (error) {
      console.warn("[appimage] Could not register the terminal command", error);
    }
  }
  if (!app.isPackaged) {
    try {
      const configuredDataHome = process.env.XDG_DATA_HOME;
      registerDevelopmentDesktop({
        dataHome:
          configuredDataHome && path.isAbsolute(configuredDataHome)
            ? configuredDataHome
            : path.join(os.homedir(), ".local", "share"),
        executable: process.execPath,
        main: path.join(__dirname, "main.js"),
        icon: path.join(__dirname, "..", "..", "public", "favicon-600x600.png"),
      });
    } catch (error) {
      console.warn(
        "[desktop] Failed to register development desktop identity",
        error,
      );
    }
  }
}

// O Electron mantém o lock de instância e o perfil Chromium dentro de
// `userData`. A versão instalada e o Electron de desenvolvimento precisam de
// perfis distintos para poderem coexistir na mesma máquina. O backend recebe
// o mesmo perfil por `VECTORA_RUNTIME_PROFILE`/`VECTORA_HOME`, isolando também
// banco, socket, PID e armazenamento do NATS.
const safeRuntimeProfile = resolveRuntimeProfile(process.env, app.isPackaged);
app.setPath(
  "userData",
  path.join(app.getPath("appData"), runtimeUserDataName(safeRuntimeProfile)),
);

const runtimeHome = resolveRuntimeHome(
  process.env,
  safeRuntimeProfile,
  os.homedir(),
);
if (!process.env.VECTORA_HOME) process.env.VECTORA_HOME = runtimeHome;

let backend: ChildProcess | null = null;
let backendShutdownPromise: Promise<void> | null = null;
let backendPidFileWrite: Promise<void> | null = null;
let shutdownResourcesDisposed = false;
let backendPort: number | null = null;
let backendPipePath: string | null = null; // Windows named pipe path (\\.\pipe\vectora-<pid>)
let backendReady = false;
let mainWindow: BrowserWindow | null = null;
let windowReadyMarkerEmitted = false;
let smokeTestWatchdog: NodeJS.Timeout | null = installedSmokeTestMode
  ? setTimeout(() => {
      console.error("Vectora startup smoke test exceeded 90 seconds.");
      app.quit();
      setTimeout(() => app.exit(124), 15_000);
    }, 90_000)
  : null;
let tray: Tray | null = null;
let pendingDeepLink: string | null = null;
// Arquivos e pastas entregues pelo sistema ("Abrir com Vectora"): ficam guardados
// até a janela montar e pedir, e depois seguem direto para ela.
const openPaths = createOpenPathsDispatcher({
  canReceive: () => Boolean(mainWindow && !mainWindow.webContents.isLoading()),
  send: (items) => mainWindow?.webContents.send("vectora:open-files", items),
  focus: () => {
    mainWindow?.show();
    mainWindow?.focus();
  },
});
let updateReady = false;
let browserViewManager: BrowserViewManager | null = null;
const desktopBridgeToken =
  process.env.VECTORA_DESKTOP_BRIDGE_TOKEN ?? randomUUID();
let selectedBackupPath: string | null = null;
let pendingBackupPromise: Promise<void> | null = null;
let updateDownloadPromise: Promise<void> | null = null;
let dmrOperations: DmrOperations | null = null;

function getDmrOperations(): DmrOperations {
  return (dmrOperations ??= new DmrOperations(
    new DockerCliExecutor(),
    app.getPath("userData"),
  ));
}

function assertTrustedRenderer(event: Electron.IpcMainInvokeEvent): void {
  if (!mainWindow || event.sender !== mainWindow.webContents) {
    throw new Error("origem IPC não autorizada");
  }
}

function startUpdateDownload(): Promise<void> {
  if (servicesDisabled())
    return Promise.reject(new Error("Services disabled (--no-services)"));
  if (updateDownloadPromise) return updateDownloadPromise;
  const pending = startUpdateDownloadAfterBackup(
    pendingBackupPromise,
    async () => {
      await autoUpdater.downloadUpdate();
    },
  );
  const tracked = pending.catch((error: unknown) => {
    if (updateDownloadPromise === tracked) updateDownloadPromise = null;
    throw error;
  });
  updateDownloadPromise = tracked;
  return tracked;
}

function pendingUpdatePath(): string {
  return path.join(app.getPath("userData"), "pending-update.json");
}

async function clearPendingUpdate(): Promise<void> {
  await fs.promises
    .rm(pendingUpdatePath(), { force: true })
    .catch(() => undefined);
}

async function rollbackPendingUpdate(): Promise<boolean> {
  try {
    const marker = JSON.parse(
      await fs.promises.readFile(pendingUpdatePath(), "utf8"),
    ) as { backupId?: string };
    if (!marker.backupId) return false;
    const userData = app.getPath("userData");
    const backupRoot = path.join(userData, "update-backups");
    const backup = (await listUpdateBackups(backupRoot)).find(
      (entry) => entry.id === marker.backupId,
    );
    if (!backup) return false;
    await restoreUpdateBackup(backup, userData, backupRoot);
    await clearPendingUpdate();
    return true;
  } catch (error) {
    console.error("[updater] rollback automático falhou", error);
    return false;
  }
}

/**
 * Browser real da aba Browser do workbench (não a SPA) — cada view é um
 * `WebContentsView`, contexto de navegação de nível superior, imune a
 * `X-Frame-Options`/`frame-ancestors` (não é uma sub-frame). Cada perfil usa
 * uma partição persistente própria, mantendo cookies/cache isolados.
 */
function getBrowserViewManager(): BrowserViewManager {
  if (browserViewManager) return browserViewManager;
  browserViewManager = new BrowserViewManager({
    createView: (
      profileId = "default",
      _kind: BrowserViewKind = "tab",
      options = {},
    ) => {
      const view = new WebContentsView({
        webPreferences: {
          contextIsolation: true,
          sandbox: true,
          nodeIntegration: false,
          session: session.fromPartition(`persist:browser-${profileId}`),
        },
      });
      attachContextMenu(view.webContents);
      if (options.zoomPercent !== undefined) {
        const factor = Math.max(0.25, Math.min(5, options.zoomPercent / 100));
        view.webContents.setZoomLevel(Math.log(factor) / Math.log(1.2));
      }
      return view as unknown as ManagedView;
    },
    attach: (view) => {
      mainWindow?.contentView.addChildView(view as unknown as WebContentsView);
    },
    destroyView: (view) => {
      mainWindow?.contentView.removeChildView(
        view as unknown as WebContentsView,
      );
    },
    emit: (viewId, event) => {
      mainWindow?.webContents.send("vectora:browser-view-event", viewId, event);
    },
    clearData: async (partition) => {
      const browserSession = session.fromPartition(partition);
      await clearBrowserSessionData(browserSession);
    },
  });
  return browserViewManager;
}

const PROTOCOL = "vectora";
const APP_SCHEME = "vectora-app"; // origem da SPA no desktop (IPC, sem TCP)
// 90s: tempo máximo para o backend responder no primeiro boot, incluindo o
// scan do Windows Defender em binário novo não reconhecido.
const READINESS_TIMEOUT_MS = 90_000;
const HEALTH_BASE_DELAY_MS = 200;
const HEALTH_MAX_DELAY_MS = 2_000;

// Evita warning barulhento quando o servidor de update responde 404/erro.
// O app já trata update failure como estado de UI; o processo não deve
// abandonar a inicialização por uma falha transitória ou release ausente.
process.on("unhandledRejection", (reason) => {
  console.warn("[updater] rejeição não tratada capturada", reason);
});

// Últimas linhas de stdout/stderr do backend — exibidas no dialog de erro.
const _backendLog: string[] = [];
const _MAX_LOG_LINES = 60;

// Arquivo que persiste o PID do sidecar backend entre sessões. Permite matar
// processo órfão deixado por crash do Electron sem disparar before-quit.
const _BACKEND_PID_FILE = path.join(runtimeHome, "backend.pid");
const signalProcessTree = (
  pid: number,
  signal: string,
  callback?: (error?: Error) => void,
) => treeKill(pid, signal, callback);

async function killStaleBackend(): Promise<void> {
  if (
    !resolveExternalBackendConnection(process.env) &&
    process.platform !== "win32" &&
    (await unixSocketIsActive(path.join(runtimeHome, "vectora.sock")))
  ) {
    throw new Error(
      `Outra instância do Vectora já está usando ${runtimeHome}. Feche-a antes de iniciar esta.`,
    );
  }
  let raw: string;
  try {
    raw = await fs.promises.readFile(_BACKEND_PID_FILE, "utf-8");
  } catch {
    // O arquivo não existe quando não houve processo anterior.
    return;
  }
  const pidText = raw.trim();
  const stalePid = /^\d+$/.test(pidText) ? Number(pidText) : Number.NaN;
  if (!Number.isSafeInteger(stalePid) || stalePid <= 0) {
    await fs.promises.unlink(_BACKEND_PID_FILE).catch(() => {});
    return;
  }
  const stopped = await stopBackendTree(
    stalePid,
    process.platform,
    signalProcessTree,
    (pid) => {
      try {
        process.kill(pid, 0);
        return true;
      } catch (error) {
        return (error as NodeJS.ErrnoException).code === "EPERM";
      }
    },
  );
  if (!stopped) {
    throw new Error(`Backend anterior (pid=${stalePid}) não encerrou`);
  }
  await fs.promises.unlink(_BACKEND_PID_FILE).catch(() => {});
}

// O scheme da SPA precisa ser registrado ANTES de app.whenReady(). Habilita
// fetch/SSE e trata a origem como segura (Secure Context: crypto.randomUUID).
protocol.registerSchemesAsPrivileged([
  {
    scheme: APP_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      stream: true,
      corsEnabled: true,
    },
  },
]);

/**
 * Transporte do backend:
 * - Linux/macOS: unix socket (~/.vectora/vectora.sock)
 * - Windows: named pipe (\\.\pipe\vectora-<pid>) lido de stdout via VECTORA_IPC_PIPE
 * - Fallback Windows (sem pipe ainda pronto): TCP loopback
 */
/** Build the IPC transport for the currently isolated runtime profile. */
function backendTransport(): http.RequestOptions {
  return backendTransportOptions(
    process.platform,
    runtimeHome,
    resolveExternalBackendConnection(process.env) ??
      (backendPipePath
        ? {
            port: backendPort,
            pipePath: backendPipePath,
            webMode: process.env.VECTORA_WEB_MODE === "1",
          }
        : null),
  );
}

const _HOP_BY_HOP = new Set([
  "transfer-encoding",
  "connection",
  "content-encoding",
  "content-length",
  "keep-alive",
]);

// Store in-memory de cookies do backend: injetado manualmente em toda request
// via forwardToBackend porque Chromium não inclui automaticamente cookies de
// session.defaultSession no Cookie header para schemes customizados (vectora-app://).
const _cookieStore = new Map<string, string>();

// Persistência entre reinicializações do app: session.defaultSession.cookies
// NÃO funciona para schemes customizados — Chromium recusa com
// "EXCLUDE_NONCOOKIEABLE_SCHEME" (cookies só são aceitos para schemes padrão
// http/https/ws/wss, mesmo com registerSchemesAsPrivileged). Sem persistência
// nenhuma, o login era perdido a cada restart do app. Grava um arquivo local
// (criptografado via safeStorage — DPAPI no Windows/Keychain no macOS — quando
// disponível) em vez de depender do cookie jar do Chromium.
const _SESSION_STORE_FILE = path.join(runtimeHome, "session.dat");

function persistCookieStore(): void {
  try {
    const json = JSON.stringify(Object.fromEntries(_cookieStore));
    const data = safeStorage.isEncryptionAvailable()
      ? safeStorage.encryptString(json)
      : Buffer.from(json, "utf-8");
    fs.mkdirSync(path.dirname(_SESSION_STORE_FILE), { recursive: true });
    fs.writeFileSync(_SESSION_STORE_FILE, data);
  } catch (err) {
    console.error("[auth] falha ao persistir sessão:", err);
  }
}

function loadPersistedCookieStore(): void {
  try {
    if (!fs.existsSync(_SESSION_STORE_FILE)) return;
    const raw = fs.readFileSync(_SESSION_STORE_FILE);
    const json = safeStorage.isEncryptionAvailable()
      ? safeStorage.decryptString(raw)
      : raw.toString("utf-8");
    const obj = JSON.parse(json) as Record<string, string>;
    for (const [name, value] of Object.entries(obj))
      _cookieStore.set(name, value);
  } catch (err) {
    console.error("[auth] falha ao carregar sessão persistida:", err);
  }
}

async function storeSetCookie(cookieStr: string): Promise<void> {
  const parsed = parseSetCookieHeader(cookieStr);
  if (!parsed) return;
  const { name, attrs, value } = parsed;

  // Max-Age=0 → deletar o cookie (logout / expiração forçada).
  if (attrs["max-age"] !== undefined && parseInt(attrs["max-age"], 10) <= 0) {
    _cookieStore.delete(name);
    persistCookieStore();
    return;
  }

  _cookieStore.set(name, value);
  persistCookieStore();
}

/**
 * Encaminha um request do scheme ``vectora-app://`` para o backend pelo
 * transporte IPC, fazendo streaming da resposta (incl. SSE). ``vectora-app://
 * app/<path>`` → backend ``/<path>``.
 */
/**
 * Caminho enviado ao backend: reconstruído contra uma origem fixa e aceito
 * somente quando continua nela, sem host nem esquema vindos do request.
 */
function backendRequestPath(source: URL): string {
  const segments = source.pathname
    .split("/")
    .filter((segment) => segment !== "")
    .map((segment) => encodeURIComponent(decodeURIComponent(segment)));
  if (segments.some((segment) => segment === "." || segment === "..")) {
    throw new Error("Caminho inválido para a ponte local.");
  }
  const query = new URLSearchParams(source.search).toString();
  return `/${segments.join("/")}${query ? `?${query}` : ""}`;
}

async function forwardToBackend(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (
    url.protocol !== `${APP_SCHEME}:` ||
    url.hostname !== "app" ||
    url.username !== "" ||
    url.password !== "" ||
    url.port !== ""
  ) {
    return new Response("Origem inválida para a ponte local.", { status: 400 });
  }
  const headers: Record<string, string> = {};
  request.headers.forEach((value, key) => {
    if (!_HOP_BY_HOP.has(key.toLowerCase())) headers[key] = value;
  });

  // Operações que executam binários ou gravam pesos são aceitas pelo backend
  // apenas quando vieram do processo Electron que abriu a sessão local.
  // O renderer nunca precisa conhecer esse segredo efêmero.
  headers["x-vectora-desktop-bridge"] = desktopBridgeToken;

  // Injeta cookies do store in-memory no header Cookie. Necessário porque
  // Chromium não inclui automaticamente cookies de session.defaultSession
  // nas requests interceptadas por protocol.handle para schemes customizados.
  if (_cookieStore.size > 0) {
    headers["cookie"] = buildCookieHeader(_cookieStore);
  }

  const body =
    request.method !== "GET" && request.method !== "HEAD"
      ? Buffer.from(await request.arrayBuffer())
      : undefined;

  return new Promise<Response>((resolve) => {
    const req = http.request(
      {
        ...backendTransport(),
        method: request.method,
        path: backendRequestPath(url),
        headers,
      },
      (res) => {
        void (async () => {
          const respHeaders = new Headers();
          for (const [key, value] of Object.entries(res.headers)) {
            if (value == null || _HOP_BY_HOP.has(key.toLowerCase())) continue;
            if (key.toLowerCase() === "set-cookie" && Array.isArray(value)) {
              for (const cookie of value) respHeaders.append(key, cookie);
            } else {
              respHeaders.set(
                key,
                Array.isArray(value) ? value.join(", ") : value,
              );
            }
          }
          // Armazena cookies explicitamente no session antes de resolver
          // para que estejam disponíveis na próxima request.
          const setCookies = res.headers["set-cookie"];
          if (Array.isArray(setCookies) && setCookies.length > 0) {
            await Promise.all(setCookies.map(storeSetCookie));
          }
          const stream = Readable.toWeb(res) as unknown as ReadableStream;
          resolve(
            new Response(stream, {
              status: res.statusCode ?? 502,
              headers: respHeaders,
            }),
          );
        })().catch((err) => {
          resolve(
            new Response(`Erro interno: ${(err as Error).message}`, {
              status: 500,
            }),
          );
        });
      },
    );
    req.on("error", (err) =>
      resolve(
        new Response(`Backend indisponível: ${err.message}`, { status: 502 }),
      ),
    );
    if (body) req.write(body);
    req.end();
  });
}

/** Health-check do backend pelo transporte IPC (UDS ou TCP). */
function pingBackend(): Promise<boolean> {
  return pingBackendHttp(backendTransport(), "/health");
}

// ---------------------------------------------------------------------------
// Backend lifecycle — spawn/path/health-check em backend-lifecycle.ts
// (extraído pra ser testável sem `electron`); aqui só a cola com o estado
// do processo main (backend, backendPort, backendPipePath, _backendLog).
// ---------------------------------------------------------------------------

const _resourcesPath = (): string =>
  process.resourcesPath || path.join(__dirname, "..");

/** Start or connect to the backend owned by this Electron profile. */
async function startBackend(): Promise<void> {
  // Modo backend-primário em dev: quando o backend Python já é o processo
  // primário (`uv run vectora start` rodado direto, fora do Electron) e se
  // autoelegeu, é ELE quem nos spawna — inverte a direção de controle que em
  // produção é sempre Electron→backend. Nesse modo não somos donos do
  // processo: não spawnamos nada, não escrevemos PID file, e `backend` fica
  // `null` pra sempre — o que já faz `restartBackend`/`before-quit`
  // no-oparem sozinhos (ambos são keyed em `backend?.pid`).
  const external = resolveExternalBackendConnection(process.env);
  if (external) {
    backendPort = external.port;
    backendPipePath = external.pipePath;
    emitSmokeStage("backend_started");
    return;
  }
  backendPort = await getFreePort();
  process.env.VECTORA_LAUNCH_MODE = "electron-primary";
  process.env.VECTORA_LAUNCH_TOKEN ??= randomUUID();
  const natsBin = natsBinaryPath(
    process.env,
    process.platform,
    _resourcesPath(),
    fs.existsSync,
  );
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    VECTORA_SOURCE_REVISION: vectoraBuildInfo.sourceRevision ?? "",
    VECTORA_SOURCE_DIRTY:
      vectoraBuildInfo.sourceDirty === null
        ? ""
        : vectoraBuildInfo.sourceDirty
          ? "1"
          : "0",
    VECTORA_RUNTIME_PROFILE: safeRuntimeProfile,
    VECTORA_HOME: runtimeHome,
    VECTORA_PORT: String(backendPort),
    VECTORA_DESKTOP: "1",
    VECTORA_DESKTOP_BRIDGE_TOKEN: desktopBridgeToken,
    VECTORA_LAUNCH_MODE: process.env.VECTORA_LAUNCH_MODE,
    VECTORA_LAUNCH_TOKEN: process.env.VECTORA_LAUNCH_TOKEN,
    ...(natsBin ? { VECTORA_NATS_BINARY: natsBin } : {}),
  };
  const launch = backendLaunch(
    process.env,
    process.platform,
    _resourcesPath(),
    path.join(__dirname, "..", "..", ".."),
  );
  backend = spawnBackendProcess(
    launch.executable,
    launch.args,
    env,
    launch.cwd,
  );
  emitSmokeStage("backend_started");
  if (backend.pid) {
    const backendPid = backend.pid;
    const pidFileWrite = fs.promises
      .mkdir(path.dirname(_BACKEND_PID_FILE), { recursive: true })
      .then(() =>
        fs.promises.writeFile(_BACKEND_PID_FILE, String(backendPid), "utf-8"),
      )
      .catch((error: unknown) => {
        console.warn("[backend] não foi possível persistir o PID", error);
      });
    backendPidFileWrite = pidFileWrite;
    void pidFileWrite.finally(() => {
      if (backendPidFileWrite === pidFileWrite) backendPidFileWrite = null;
    });
  }
  const pipeParser = new IpcPipeParser();
  backend.stdout?.on("data", (b: Buffer) => {
    const text = b.toString();
    // Lê VECTORA_IPC_PIPE=<path> do stdout para usar named pipe no Windows —
    // acumula chunks até fechar linha, protegendo contra write parcial do
    // kernel fatiar o marcador entre dois eventos "data".
    const pipe = pipeParser.push(text);
    if (pipe) backendPipePath = pipe;
    _backendLog.push(text);
    if (_backendLog.length > _MAX_LOG_LINES) _backendLog.shift();
    process.stdout.write(`[backend] ${text}`);
  });
  backend.stderr?.on("data", (b: Buffer) => {
    const text = b.toString();
    _backendLog.push(text);
    if (_backendLog.length > _MAX_LOG_LINES) _backendLog.shift();
    process.stderr.write(`[backend] ${text}`);
  });
  backend.on("exit", handleBackendExit);
}

function handleBackendExit(code: number | null, signal: NodeJS.Signals | null) {
  if ((app as unknown as { isQuitting: boolean }).isQuitting) return;
  backendReady = false;
  const logs = _backendLog.slice(-15).join("").trim();
  const detail = `code=${code} signal=${signal ?? "none"}${logs ? `\n\n${logs}` : ""}`;
  console.error(`[backend] saiu inesperadamente: ${detail}`);
  const opts = {
    type: "error" as const,
    title: "Vectora encerrou inesperadamente",
    message: "O backend Vectora encerrou sem aviso.",
    detail,
    buttons: ["Reiniciar", "Sair"],
    defaultId: 0,
    cancelId: 1,
  };
  const action = mainWindow
    ? dialog.showMessageBoxSync(mainWindow, opts)
    : dialog.showMessageBoxSync(opts);
  if (action === 0) {
    void restartBackend();
  } else {
    app.quit();
  }
}

async function restartBackend(): Promise<void> {
  backendReady = false;
  try {
    if (backend?.pid) {
      const previousBackend = backend;
      backend = null;
      const stopped = await stopBackendProcess(
        previousBackend,
        process.platform,
        signalProcessTree,
      );
      if (!stopped) {
        throw new Error("O backend anterior não encerrou após SIGKILL");
      }
      try {
        fs.unlinkSync(_BACKEND_PID_FILE);
      } catch {}
    }
    backendPipePath = null;
    await startBackend();
    await waitForBackend();
    const handshakeMode =
      process.env.VECTORA_EXTERNAL_BACKEND === "1"
        ? "python-primary"
        : "electron-primary";
    const handshakeToken = process.env.VECTORA_LAUNCH_TOKEN ?? "";
    if (
      !(await signLaunchHandshake(
        backendTransport(),
        handshakeMode,
        handshakeToken,
      ))
    ) {
      throw new Error("O backend recusou a assinatura do modo de launch");
    }
    backendReady = true;
    if (mainWindow) {
      void mainWindow.loadURL(`${APP_SCHEME}://app/`);
    } else {
      createWindow();
    }
  } catch (err) {
    if (await rollbackPendingUpdate()) {
      requestElectronRestart();
      return;
    }
    dialog.showErrorBox(
      "Vectora",
      `Falha ao reiniciar backend: ${(err as Error).message}`,
    );
    app.exit(1);
  }
}

/**
 * Health-check com backoff exponencial. Cada falha dobra o delay até
 * ``HEALTH_MAX_DELAY_MS``, capado no ``READINESS_TIMEOUT_MS`` global.
 * Falha imediatamente se o processo backend já terminou (crash no startup).
 */
async function waitForBackend(): Promise<void> {
  await waitForBackendReady({
    ping: pingBackend,
    isExited: () => ({
      exited: backend !== null && backend.exitCode !== null,
      code: backend?.exitCode ?? null,
    }),
    timeoutMs: READINESS_TIMEOUT_MS,
    baseDelayMs: HEALTH_BASE_DELAY_MS,
    maxDelayMs: HEALTH_MAX_DELAY_MS,
    getRecentLogs: () => _backendLog.slice(-20).join("").trim(),
  });
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------

/**
 * Menu do clique direito. O Electron não abre menu algum sozinho: sem isto não
 * há como recortar, copiar, colar, selecionar tudo nem corrigir uma palavra.
 * Vale para a janela principal e para as páginas do navegador embutido.
 */
function attachContextMenu(webContents: WebContents): void {
  webContents.on("context-menu", (_event, params) => {
    const template = buildContextMenuTemplate(
      params,
      contextMenuLabels(app.getLocale()),
      {
        replaceMisspelling: (suggestion) =>
          webContents.replaceMisspelling(suggestion),
        addToDictionary: (word) =>
          webContents.session.addWordToSpellCheckerDictionary(word),
        copyText: (text) => clipboard.writeText(text),
        reload: () => webContents.reload(),
        inspect: app.isPackaged
          ? undefined
          : () => webContents.inspectElement(params.x, params.y),
      },
    );
    if (template.length === 0) return;
    Menu.buildFromTemplate(template).popup({
      window:
        BrowserWindow.fromWebContents(webContents) ?? mainWindow ?? undefined,
    });
  });
}

/**
 * A janela é sem moldura (`frame: false`): maximizada por alguns gerenciadores de
 * janelas do Linux ela sobra além da tela, e os botões da barra de título (fechar
 * incluído) ficam para fora. Depois de maximizar ou restaurar, encaixa os bounds
 * na área útil do monitor onde ela está.
 */
let fitTimers: NodeJS.Timeout[] = [];

function fitWindowToWorkArea(): void {
  const win = mainWindow;
  if (!win || win.isDestroyed()) return;
  // O compositor pode entregar os bounds finais depois do evento; confere em
  // vários instantes e cancela a verificação anterior para não acumular.
  fitTimers.forEach(clearTimeout);
  fitTimers = [80, 250, 700].map((delay) =>
    setTimeout(() => {
      if (win.isDestroyed() || win.isFullScreen() || win.isMinimized()) return;
      const bounds = win.getBounds();
      const fitted = fitBoundsToWorkArea(
        bounds,
        screen.getDisplayMatching(bounds).workArea,
      );
      if (fitted) win.setBounds(fitted);
    }, delay),
  );
}

function activateMainWindow(reason: string, visible: boolean): void {
  if (!visible || process.env.VECTORA_WEB_MODE === "1") return;
  const window = mainWindow;
  if (!window || window.isDestroyed()) return;

  window.show();
  window.focus();
  app.focus();
  console.info("Janela do Vectora ativada", {
    reason,
    visible: window.isVisible(),
    focused: window.isFocused(),
    bounds: window.getBounds(),
  });
  if (installedSmokeTestMode && reason !== "did-finish-load") return;
  // Lido por backend/services/electron_sidecar.py (WINDOW_READY_MARKER).
  // A emissão é idempotente porque vários eventos podem ativar a mesma janela.
  if (!windowReadyMarkerEmitted) {
    windowReadyMarkerEmitted = true;
    console.log("VECTORA_ELECTRON_WINDOW_READY");
    emitSmokeStage("window_ready");
    if (installedSmokeTestMode) {
      if (smokeTestWatchdog) clearTimeout(smokeTestWatchdog);
      console.log("Vectora startup smoke test passed; closing the app.");
      const shutdownWatchdog = setTimeout(() => {
        console.error(
          "Vectora startup smoke test could not shut down cleanly.",
        );
        app.exit(1);
      }, 15_000);
      app.once("will-quit", () => clearTimeout(shutdownWatchdog));
      app.once("will-quit", () => emitSmokeStage("shutdown_complete"));
      app.quit();
    }
  }
}

function createWindow(options: { visible?: boolean } = {}): void {
  const webMode = process.env.VECTORA_WEB_MODE === "1";
  const shouldShow = options.visible ?? !webMode;
  if (mainWindow) {
    activateMainWindow("existing-window", shouldShow);
    return;
  }
  if (!backendReady) throw new Error("Backend ainda não está pronto.");
  // Janela nasce proporcional à tela detectada, não num tamanho fixo — numa
  // tela 1080p+ um default fixo como 1400x900 já nasce quase em tela cheia,
  // prejudicando o modo janela (ver computeDefaultWindowSize).
  const { width, height } = computeDefaultWindowSize(
    screen.getPrimaryDisplay().workAreaSize,
  );
  const visible = shouldShow;
  windowReadyMarkerEmitted = false;
  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: 800,
    minHeight: 600,
    // O modo desktop deve aparecer assim que o BrowserWindow é criado. Usar
    // show:false deixava a janela presa atrás de ready-to-show quando o GPU
    // process do Chromium atrasava ou falhava no Linux.
    show: visible,
    autoHideMenuBar: true,
    icon: resolveAppIcon(),
    // Titlebar customizada (estilo VS Code, ver src/components/layout/title-bar.tsx
    // no frontend) — a janela nativa não desenha min/max/close nem título; o
    // renderer é responsável pelos controles e pela região arrastável.
    frame: false,
    backgroundColor: "#0a0e1a",
    webPreferences: {
      offscreen: webMode,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  attachContextMenu(mainWindow.webContents);

  mainWindow.webContents.on(
    "did-fail-load",
    (_event, code, description, url, isMainFrame) => {
      console.error("Falha ao carregar a interface do Vectora", {
        code,
        description,
        url,
        isMainFrame,
      });
    },
  );
  mainWindow.webContents.on("render-process-gone", (_event, details) => {
    console.error("Renderer do Vectora encerrou", details);
  });
  mainWindow.webContents.on(
    "console-message",
    (_event, level, message, line, sourceId) => {
      console.info("[renderer]", { level, message, line, sourceId });
    },
  );

  mainWindow.on("maximize", () => {
    mainWindow?.webContents.send("vectora:window-state", { maximized: true });
    fitWindowToWorkArea();
  });
  mainWindow.on("unmaximize", () => {
    mainWindow?.webContents.send("vectora:window-state", { maximized: false });
    fitWindowToWorkArea();
  });
  // Maximizar pelo gerenciador de janelas nem sempre emite "maximize" antes
  // de os bounds mudarem: qualquer mudança de tamanho, restauração ou troca de
  // monitor também confere se a janela coube na área útil.
  mainWindow.on("resize", fitWindowToWorkArea);
  mainWindow.on("restore", fitWindowToWorkArea);
  mainWindow.on("leave-full-screen", fitWindowToWorkArea);
  mainWindow.on("moved", fitWindowToWorkArea);

  // Inject app version no preload sem precisar recompilar.
  process.env.VECTORA_APP_VERSION = app.getVersion();

  // Carrega a SPA pela origem IPC — zero porta TCP exposta (UDS em Linux/macOS).
  const activateAfterLoad = (reason: string) => {
    activateMainWindow(reason, visible);
  };
  mainWindow.once("ready-to-show", () => activateAfterLoad("ready-to-show"));
  // Em Linux, falhas/atrasos do GPU process podem impedir ready-to-show mesmo
  // com o renderer carregando normalmente. Esses eventos são um fallback
  // seguro: a janela só aparece depois que a página começou/terminou de carregar.
  mainWindow.webContents.once("dom-ready", () => {
    emitSmokeStage("renderer_dom_ready");
    activateAfterLoad("dom-ready");
  });
  mainWindow.webContents.once("did-finish-load", () => {
    emitSmokeStage("renderer_loaded");
    activateAfterLoad("did-finish-load");
  });
  const visibilityFallback = setTimeout(
    () => activateAfterLoad("visibility-timeout"),
    2_000,
  );
  mainWindow.once("closed", () => clearTimeout(visibilityFallback));
  void mainWindow.loadURL(`${APP_SCHEME}://app/`).catch((error) => {
    console.error("Falha ao iniciar a interface do Vectora", error);
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
    windowReadyMarkerEmitted = false;
  });

  // Links externos abrem no navegador padrão (Stripe portal, dashboard).
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  // Bloqueia reload/devtools em prod. Permite com VECTORA_DEV=1.
  if (process.env.VECTORA_DEV !== "1") {
    mainWindow.webContents.on("before-input-event", (event, input) => {
      const isReload =
        (input.control || input.meta) && input.key.toLowerCase() === "r";
      const isDevTools =
        (input.control || input.meta) &&
        input.shift &&
        input.key.toLowerCase() === "i";
      if (isReload || isDevTools) event.preventDefault();
    });
  }
}

// ---------------------------------------------------------------------------
// Tray icon
// ---------------------------------------------------------------------------

function getTrayIcon(): Electron.NativeImage {
  // electron-builder copia frontend/public/favicon-32x32.png pra
  // resources/tray-icon.png (ver electron-builder.yml, extraResources); em
  // dev usa o favicon do frontend direto da fonte como fallback.
  const candidates = [
    path.join(process.resourcesPath, "tray-icon.png"),
    path.join(__dirname, "..", "..", "public", "favicon-32x32.png"),
  ];
  for (const c of candidates) {
    const img = nativeImage.createFromPath(c);
    if (!img.isEmpty()) return img;
  }
  return nativeImage.createEmpty();
}

/** Resolve o ícone nativo da janela a partir dos recursos empacotados ou do source. */
function resolveAppIcon(): string | undefined {
  const isDev = !app.isPackaged;
  if (process.platform === "linux") {
    const candidates = [
      ...(isDev
        ? [path.join(__dirname, "..", "..", "public", "favicon-600x600.png")]
        : [path.join(process.resourcesPath, "vectora-icon.png")]),
      path.join(__dirname, "..", "..", "public", "favicon-600x600.png"),
    ];
    return candidates.find((candidate) => fs.existsSync(candidate));
  }

  const candidates = [
    path.join(process.resourcesPath || "", "vectora-icon.png"),
    path.join(process.resourcesPath || "", "vectora.ico"),
    path.join(__dirname, "..", "..", "public", "favicon-600x600.png"),
    path.join(__dirname, "..", "..", "public", "vectora.ico"), // dev
  ];
  return candidates.find((c) => fs.existsSync(c));
}

function createTray(): void {
  if (tray) return;
  tray = new Tray(getTrayIcon());
  tray.setToolTip("Vectora");
  refreshTrayMenu();
  tray.on("click", () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) mainWindow.focus();
      else mainWindow.show();
    } else {
      createWindow();
    }
  });
}

function refreshTrayMenu(): void {
  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    {
      label: "Abrir Vectora",
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      },
    },
    {
      label: "Reiniciar Vectora",
      click: () => {
        requestElectronRestart();
      },
    },
    ...(updateReady
      ? [
          { type: "separator" as const },
          {
            label: "Aplicar atualização e reiniciar",
            click: () => autoUpdater.quitAndInstall(),
          },
        ]
      : []),
    { type: "separator" },
    {
      label: "Sair",
      click: () => app.quit(),
    },
  ]);
  tray.setContextMenu(menu);
}

// ---------------------------------------------------------------------------
// Deep-link (vectora://)
// ---------------------------------------------------------------------------

function registerDeepLinkProtocol(): void {
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [
      path.resolve(process.argv[1]),
    ]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL);
  }
}

function deliverDeepLink(url: string): void {
  if (mainWindow && !mainWindow.webContents.isLoading()) {
    mainWindow.webContents.send("vectora:deep-link", url);
    mainWindow.show();
    mainWindow.focus();
  } else {
    pendingDeepLink = url;
  }
}

// ---------------------------------------------------------------------------
// Auto-updater
// ---------------------------------------------------------------------------

/**
 * Lê `GET /settings/prefs` (mesmo endpoint que o settings-store do frontend
 * usa via `PATCH`/`GET`, chave `autoUpdateEnabled` em
 * `_ALLOWED_FRONTEND_PREF_KEYS`, `backend/workspace/runtime_settings.py`)
 * pra decidir se agenda as checagens automáticas periódicas. Falha (rede,
 * JSON inválido, campo ausente) → `true` (fail-open, mesmo default do
 * settings-store) — nunca deixa o usuário sem update por causa de um erro
 * de leitura transitório.
 */
async function isAutoUpdateEnabled(): Promise<boolean> {
  const prefs = await fetchBackendJson<{ autoUpdateEnabled?: boolean }>(
    backendTransport(),
    "/settings/prefs",
  );
  return prefs?.autoUpdateEnabled !== false;
}

/** Resolve changelog notes from the packaged manifest, using the authenticated backend as fallback. */
async function fetchPackagedChangelog(
  fallback: string,
  expectedVersion: string,
): Promise<string> {
  const packaged = fallback.trim();
  if (packaged) return packaged;
  try {
    const payload = await fetchBackendJson<{
      notes?: string;
      version?: string;
    }>(
      backendTransport(),
      "/api/updates/changelog",
      _cookieStore.size > 0 ? { cookie: buildCookieHeader(_cookieStore) } : {},
    );
    if (payload?.version !== expectedVersion) return "";
    return payload.notes?.trim() || "";
  } catch (error) {
    console.warn("[updater] não foi possível carregar o changelog", error);
    return fallback;
  }
}

function servicesDisabled(): boolean {
  return ["1", "true", "yes", "on"].includes(
    (process.env.VECTORA_NO_SERVICES ?? "").trim().toLowerCase(),
  );
}

/**
 * Registra os listeners do `electron-updater` e propaga o estado pro
 * renderer via `vectora:update-status` (consumido em `UpdateBanner`,
 * `frontend/components/layout/update-banner.tsx`). Sempre chamado — inclusive
 * com auto-update desligado nas Preferências — porque uma checagem manual
 * (`vectora:check-for-update`, ver `registerIpc()`) também precisa desses
 * listeners pra a UI mostrar o resultado.
 */
function setupAutoUpdater(): void {
  if (servicesDisabled()) {
    // Registrado no log do sidecar: confirma que a flag chegou ao Electron.
    console.info(
      "[updater] desativado nesta execução: Services desligados (--no-services)",
    );
    return;
  }
  // O binário npm do Electron usado em desenvolvimento informa a versão
  // `0.0`; electron-updater exige semver válida e aborta o boot ao inicializar.
  // Atualizações automáticas só fazem sentido no pacote instalado.
  if (!app.isPackaged) {
    console.info("[updater] desativado fora do pacote instalado");
    return;
  }

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  let latestStatus: UpdateStatus = { state: "not-available" };

  const broadcast = (status: Partial<UpdateStatus>) => {
    latestStatus = { ...latestStatus, ...status };
    mainWindow?.webContents.send("vectora:update-status", latestStatus);
  };

  autoUpdater.on("checking-for-update", () => broadcast({ state: "checking" }));
  autoUpdater.on("update-available", (info) => {
    const notes = Array.isArray(info.releaseNotes)
      ? info.releaseNotes
          .map((note) => note.note)
          .filter(Boolean)
          .join("\n")
      : (info.releaseNotes ?? "");
    updateDownloadPromise = null;
    pendingBackupPromise = createRotatingUpdateBackup(
      app.getPath("userData"),
      path.join(app.getPath("userData"), "update-backups"),
      app.getVersion(),
    ).then(async (backup) => {
      await fs.promises.writeFile(
        pendingUpdatePath(),
        JSON.stringify({ version: info.version, backupId: backup.id }, null, 2),
        { mode: 0o600 },
      );
    });
    broadcast({ state: "available", message: info.version, changelog: notes });
    void fetchPackagedChangelog(notes, info.version).then((changelog) => {
      if (changelog && changelog !== notes) {
        // Atualiza somente as notas. Preserva `downloading`/`downloaded` caso
        // a resposta do backend chegue depois do progresso do download.
        broadcast({ changelog });
      }
    });
    void startUpdateDownload().catch((error: unknown) => {
      broadcast({
        state: "error",
        message: `Download da atualização falhou: ${String(error)}`,
      });
    });
  });
  autoUpdater.on("update-not-available", () =>
    broadcast({ state: "not-available" }),
  );
  autoUpdater.on("download-progress", (p) =>
    broadcast({ state: "downloading", progress: p.percent }),
  );
  autoUpdater.on("update-downloaded", () => {
    updateReady = true;
    refreshTrayMenu();
    broadcast({ state: "downloaded" });
  });
  autoUpdater.on("error", (err) =>
    broadcast({ state: "error", message: err.message }),
  );
}

async function safeCheckForUpdates(): Promise<void> {
  if (servicesDisabled()) return;
  // The npm Electron binary reports version 0.0 in development; the updater
  // is meaningful only for a packaged build with a release semver.
  if (!app.isPackaged) return;
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    console.warn("[updater] checkForUpdates falhou", err);
  }
}

/**
 * Agenda as checagens automáticas periódicas: 30s após boot (deixa o backend
 * subir primeiro) e depois a cada 6h. Só chamada quando `autoUpdateEnabled`
 * está ligado (Preferências → Atualizações) — a checagem manual continua
 * disponível independente disso, ver `registerIpc()`.
 */
function scheduleAutoUpdateChecks(): void {
  if (servicesDisabled()) return;
  setTimeout(() => {
    void safeCheckForUpdates();
  }, 30_000);
  setInterval(() => void safeCheckForUpdates(), 6 * 60 * 60 * 1000);
}

// ---------------------------------------------------------------------------
// IPC handlers (responde ao preload bridge)
// ---------------------------------------------------------------------------

function registerIpc(): void {
  ipcMain.handle("vectora:open-external", (_event, url: string) =>
    shell.openExternal(url),
  );
  // Seletor nativo de pasta (Pastas Seguras). Devolve `null` no cancelamento —
  // o renderer distingue "cancelou" de "escolheu" pra não limpar o campo.
  ipcMain.handle("vectora:pick-directory", async () => {
    const opts: Electron.OpenDialogOptions = {
      properties: ["openDirectory", "createDirectory"],
    };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, opts)
      : await dialog.showOpenDialog(opts);
    if (result.canceled || result.filePaths.length === 0) return null;
    return result.filePaths[0];
  });
  ipcMain.handle("vectora:backup-pick-and-inspect", async () => {
    // Starting a new selection invalidates any previously inspected archive,
    // including when the native dialog is cancelled.
    selectedBackupPath = null;
    const opts: Electron.OpenDialogOptions = {
      properties: ["openFile"],
      filters: [{ name: "Backup Vectora", extensions: ["zip", "gz"] }],
    };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, opts)
      : await dialog.showOpenDialog(opts);
    if (result.canceled || result.filePaths.length === 0) return null;
    const archivePath = result.filePaths[0];
    try {
      const preview = await postBackendJson(
        backendTransport(),
        "/storage/backup/inspect",
        { archive_path: archivePath },
        { "x-vectora-desktop-bridge": desktopBridgeToken },
      );
      if (!preview) return null;
      selectedBackupPath = archivePath;
      return preview;
    } finally {
      if (!selectedBackupPath) selectedBackupPath = null;
    }
  });
  ipcMain.handle(
    "vectora:backup-restore",
    async (_event, categories: string[]) => {
      if (!selectedBackupPath) return null;
      try {
        return await postBackendJson(
          backendTransport(),
          "/storage/backup/restore",
          { archive_path: selectedBackupPath, categories, confirmed: true },
          { "x-vectora-desktop-bridge": desktopBridgeToken },
        );
      } finally {
        selectedBackupPath = null;
      }
    },
  );
  ipcMain.handle("vectora:capture-screenshot", async () => {
    const sources = await desktopCapturer.getSources({
      types: ["screen", "window"],
      thumbnailSize: { width: 1920, height: 1080 },
    });
    if (sources.length === 0) return null;
    const result = mainWindow
      ? await dialog.showMessageBox(mainWindow, {
          type: "question",
          buttons: [...sources.map((source) => source.name), "Cancelar"],
          cancelId: sources.length,
          title: "Capturar screenshot",
          message: "Escolha a tela ou janela que deseja anexar.",
        })
      : await dialog.showMessageBox({
          type: "question",
          buttons: [...sources.map((source) => source.name), "Cancelar"],
          cancelId: sources.length,
          title: "Capturar screenshot",
          message: "Escolha a tela ou janela que deseja anexar.",
        });
    if (result.response === sources.length) return null;
    const source = sources[result.response];
    return source ? source.thumbnail.toPNG() : null;
  });
  // A janela pede os arquivos que chegaram antes de ela montar e passa a receber os
  // seguintes por "vectora:open-files".
  ipcMain.handle("vectora:take-deep-link", () => {
    const url = pendingDeepLink;
    pendingDeepLink = null;
    return url;
  });
  ipcMain.handle("vectora:take-open-files", () => openPaths.take());
  ipcMain.on("vectora:deep-link-ack", (_event, url: string) => {
    console.log(`[deep-link] renderer ack: ${url}`);
  });
  ipcMain.on("vectora:quit-and-install", () => {
    if (updateReady) autoUpdater.quitAndInstall();
  });
  // Checagem manual (botão "Verificar atualização agora", Preferências) —
  // independente do toggle de auto-update, que só gate os timers
  // periódicos em scheduleAutoUpdateChecks().
  ipcMain.on("vectora:check-for-update", () => {
    void safeCheckForUpdates();
  });
  ipcMain.handle("vectora:list-update-backups", () =>
    listUpdateBackups(path.join(app.getPath("userData"), "update-backups")),
  );
  ipcMain.handle("vectora:restore-update-backup", (_event, backup: unknown) =>
    restoreUpdateBackup(
      backup as Parameters<typeof restoreUpdateBackup>[0],
      app.getPath("userData"),
      path.join(app.getPath("userData"), "update-backups"),
    ),
  );
  ipcMain.on("vectora:download-update", () => {
    void startUpdateDownload().catch((error: unknown) => {
      console.warn("[updater] downloadUpdate falhou", error);
    });
  });

  // Menu de clique direito desenhado pelo Electron para itens que a interface
  // escolhe (abas, por exemplo): devolve o id do item ou null se fechou sem escolher.
  ipcMain.handle("vectora:context-menu-popup", (event, rawItems: unknown) => {
    if (!mainWindow || event.sender !== mainWindow.webContents) return null;
    const items = sanitizePopupItems(rawItems);
    if (!items) return null;
    return new Promise<string | null>((resolve) => {
      let picked: string | null = null;
      const menu = Menu.buildFromTemplate(
        buildPopupTemplate(items, (id) => {
          picked = id;
        }),
      );
      menu.popup({
        window: mainWindow ?? undefined,
        callback: () => resolve(picked),
      });
    });
  });

  // Controles da titlebar customizada (frame: false — ver createWindow()).
  ipcMain.on("vectora:window-minimize", () => mainWindow?.minimize());
  ipcMain.on("vectora:window-maximize-toggle", () => {
    if (!mainWindow) return;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  });
  ipcMain.on("vectora:window-close", () => mainWindow?.close());
  ipcMain.handle(
    "vectora:window-is-maximized",
    () => mainWindow?.isMaximized() ?? false,
  );

  // WebSocket não passa pelo protocol.handle(APP_SCHEME, forwardToBackend)
  // (só cobre fetch/HTTP) — o renderer carrega de vectora-app://, um scheme
  // custom contra o qual `new WebSocket("/caminho")` não resolve pra ws://
  // como resolveria numa origem http:// normal. O backend sempre escuta em
  // TCP loopback (mesmo quando o transporte HTTP principal é UDS/named
  // pipe), então expõe essa origem pro renderer construir URLs absolutas.
  ipcMain.handle("vectora:get-backend-ws-origin", () =>
    backendPort !== null ? `ws://127.0.0.1:${backendPort}` : null,
  );

  // Browser real da aba Browser (ver getBrowserViewManager) — comandos
  // invoke/handle (resposta esperada) + eventos fire-and-forget, mesmo
  // padrão do restante deste arquivo.
  ipcMain.handle("vectora:browser-create-view", (event, options: unknown) => {
    if (!isTrustedBrowserSender(event)) throw new Error("origem IPC inválida");
    if (!options || typeof options !== "object")
      throw new Error("opções inválidas");
    const { profileId, kind, allowPopups, zoomPercent } = options as Partial<{
      profileId: string;
      kind: BrowserViewKind;
      allowPopups: boolean;
      zoomPercent: number;
    }>;
    if (typeof profileId !== "string" || !isValidProfileId(profileId)) {
      throw new Error("profileId inválido");
    }
    if (!isValidBrowserViewKind(kind)) throw new Error("kind inválido");
    if (allowPopups !== undefined && typeof allowPopups !== "boolean") {
      throw new Error("allowPopups inválido");
    }
    if (
      zoomPercent !== undefined &&
      (typeof zoomPercent !== "number" || !Number.isFinite(zoomPercent))
    ) {
      throw new Error("zoomPercent inválido");
    }
    return getBrowserViewManager().createView(
      profileId,
      kind,
      browserOwnerId(event),
      { allowPopups, zoomPercent },
    );
  });
  ipcMain.on("vectora:browser-destroy-view", (event, viewId: number) => {
    if (!isTrustedBrowserSender(event)) return;
    if (!isValidViewId(viewId)) return;
    getBrowserViewManager().destroyView(viewId, browserOwnerId(event));
  });
  ipcMain.handle(
    "vectora:browser-navigate",
    (event, viewId: number, url: string) => {
      if (!isTrustedBrowserSender(event))
        return { ok: false, error: "origem IPC inválida" };
      if (!isValidViewId(viewId) || !isValidBrowserUrl(url)) {
        return { ok: false, error: "argumentos inválidos" };
      }
      return getBrowserViewManager().navigate(
        viewId,
        url,
        browserOwnerId(event),
      );
    },
  );
  ipcMain.on("vectora:browser-go-back", (event, viewId: number) => {
    if (!isTrustedBrowserSender(event)) return;
    if (!isValidViewId(viewId)) return;
    getBrowserViewManager().goBack(viewId, browserOwnerId(event));
  });
  ipcMain.on("vectora:browser-go-forward", (event, viewId: number) => {
    if (!isTrustedBrowserSender(event)) return;
    if (!isValidViewId(viewId)) return;
    getBrowserViewManager().goForward(viewId, browserOwnerId(event));
  });
  ipcMain.on("vectora:browser-reload", (event, viewId: number) => {
    if (!isTrustedBrowserSender(event)) return;
    if (!isValidViewId(viewId)) return;
    getBrowserViewManager().reload(viewId, browserOwnerId(event));
  });
  ipcMain.on("vectora:browser-stop", (event, viewId: number) => {
    if (!isTrustedBrowserSender(event)) return;
    if (!isValidViewId(viewId)) return;
    getBrowserViewManager().stop(viewId, browserOwnerId(event));
  });
  ipcMain.on(
    "vectora:browser-set-bounds",
    (event, viewId: number, bounds: ViewBounds) => {
      if (!isTrustedBrowserSender(event)) return;
      if (!isValidViewId(viewId) || !isValidViewBounds(bounds)) return;
      getBrowserViewManager().setBounds(viewId, bounds, browserOwnerId(event));
    },
  );
  ipcMain.handle(
    "vectora:browser-clear-profile-data",
    (event, profileId?: string) => {
      if (!isTrustedBrowserSender(event))
        throw new Error("origem IPC inválida");
      if (profileId !== undefined && !isValidProfileId(profileId)) {
        throw new Error("profileId inválido");
      }
      return getBrowserViewManager().clearData(profileId);
    },
  );
  ipcMain.on(
    "vectora:browser-set-visible",
    (event, viewId: number, visible: boolean) => {
      if (!isTrustedBrowserSender(event)) return;
      if (!isValidViewId(viewId) || typeof visible !== "boolean") return;
      getBrowserViewManager().setVisible(
        viewId,
        visible,
        browserOwnerId(event),
      );
    },
  );
  ipcMain.on(
    "vectora:browser-set-zoom",
    (event, viewId: number, percent: number) => {
      if (!isTrustedBrowserSender(event)) return;
      if (
        !isValidViewId(viewId) ||
        typeof percent !== "number" ||
        !Number.isFinite(percent)
      )
        return;
      getBrowserViewManager().setZoomPercent(
        viewId,
        percent,
        browserOwnerId(event),
      );
    },
  );
  ipcMain.on(
    "vectora:browser-set-allow-popups",
    (event, viewId: number, allowPopups: boolean) => {
      if (!isTrustedBrowserSender(event)) return;
      if (!isValidViewId(viewId) || typeof allowPopups !== "boolean") return;
      getBrowserViewManager().setAllowPopups(
        viewId,
        allowPopups,
        browserOwnerId(event),
      );
    },
  );

  // Docker Model Runner: o renderer só recebe contratos tipados; o CLI fica
  // exclusivamente no processo principal e exige a origem IPC da janela.
  ipcMain.handle("vectora:dmr-detect", (event) => {
    assertTrustedRenderer(event);
    return getDmrOperations().detect();
  });
  ipcMain.handle("vectora:dmr-install-runner", (event) => {
    assertTrustedRenderer(event);
    return getDmrOperations().ensureRunner();
  });
  ipcMain.handle("vectora:dmr-list", (event) => {
    assertTrustedRenderer(event);
    return getDmrOperations().list();
  });
  ipcMain.handle("vectora:dmr-prepare", (event, reference: string) => {
    assertTrustedRenderer(event);
    return getDmrOperations().prepare(reference);
  });
  ipcMain.handle(
    "vectora:dmr-start",
    (event, reference: string, contextSize?: number) => {
      assertTrustedRenderer(event);
      return getDmrOperations().start(reference, contextSize);
    },
  );
  ipcMain.handle("vectora:dmr-stop", (event, reference: string) => {
    assertTrustedRenderer(event);
    return getDmrOperations().stop(reference);
  });
  ipcMain.handle(
    "vectora:dmr-remove",
    (event, reference: string, confirmed: boolean) => {
      assertTrustedRenderer(event);
      return getDmrOperations().remove(reference, confirmed);
    },
  );
  ipcMain.handle("vectora:dmr-operation", (event, id: string) => {
    assertTrustedRenderer(event);
    return getDmrOperations().getOperation(id) ?? null;
  });
  ipcMain.handle("vectora:dmr-operations", (event) => {
    assertTrustedRenderer(event);
    return getDmrOperations().listOperations();
  });
  ipcMain.handle("vectora:dmr-cancel", (event, id: string) => {
    assertTrustedRenderer(event);
    return getDmrOperations().cancel(id);
  });

  // Instalação de temas do VS Code Marketplace (Preferências → Aparência) —
  // download + extração rodam aqui (fora do sandbox do renderer); erros
  // propagam pro renderer via rejeição da Promise do invoke.
  ipcMain.handle(
    "vectora:themes-fetch-marketplace",
    (_event, extensionId: string) => fetchMarketplaceThemes(extensionId),
  );
  ipcMain.handle(
    "vectora:themes-search-marketplace",
    (_event, query: string, limit?: number) =>
      searchMarketplaceThemes(query, limit),
  );

  // Zoom nativo (Preferências → Aparência → UI Scale) — mais nítido que o
  // fallback CSS usado no navegador. `factor = 1.2^level` é a própria
  // conversão do Chromium entre zoomLevel e fator visual.
  ipcMain.on("vectora:set-zoom-percent", (_event, percent: number) => {
    if (!mainWindow) return;
    const factor = Math.max(0.5, Math.min(2, percent / 100));
    mainWindow.webContents.setZoomLevel(Math.log(factor) / Math.log(1.2));
  });
  ipcMain.handle("vectora:get-zoom-percent", () => {
    if (!mainWindow) return 100;
    const factor = Math.pow(1.2, mainWindow.webContents.getZoomLevel());
    return Math.round(factor * 100);
  });
}

// ---------------------------------------------------------------------------
// Single instance lock — necessário para deep-link no Windows/Linux
// ---------------------------------------------------------------------------

// O modo web é um sidecar do backend, não uma sessão interativa do app. Ele
// pode coexistir com uma instância desktop antiga sem herdar o lock global do
// Electron e encerrar o servidor HTTP por engano.
const webSidecarMode = process.env.VECTORA_WEB_MODE === "1";
const gotLock =
  installedTestsMode || webSidecarMode || app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

app.on("second-instance", (_event, argv, workingDirectory) => {
  // Windows passa o deep-link como último argumento.
  const url = argv.find((a) => a.startsWith(`${PROTOCOL}://`));
  if (url) deliverDeepLink(url);
  openPaths.enqueue(
    pathsFromArgv(argv, {
      cwd: workingDirectory,
      defaultApp: process.defaultApp,
    }),
  );
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  } else if (backendReady) {
    createWindow({ visible: process.env.VECTORA_WEB_MODE !== "1" });
  }
});

// Arquivos e pastas passados na primeira execução ("Abrir com Vectora", arrastar
// sobre o ícone, linha de comando).
if (gotLock && !installedTestsMode && !webSidecarMode) {
  openPaths.enqueue(
    pathsFromArgv(process.argv, { defaultApp: process.defaultApp }),
  );
}

// macOS entrega os arquivos por "open-file", inclusive antes de o app estar pronto.
app.on("open-file", (event, filePath) => {
  event.preventDefault();
  openPaths.enqueue([filePath]);
});

// macOS entrega via "open-url".
app.on("open-url", (event, url) => {
  event.preventDefault();
  if (url.startsWith(`${PROTOCOL}://`)) deliverDeepLink(url);
});

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

(app as unknown as { isQuitting: boolean }).isQuitting = false;

app.whenReady().then(async () => {
  if (!gotLock) return;
  emitSmokeStage("electron_ready");
  if (installedSmokeTestMode) {
    console.log(
      `VECTORA_EVAL_APP:${app.getVersion()}:${process.versions.electron}:${process.versions.chrome}:${vectoraBuildInfo.sourceRevision ?? "unknown"}:${vectoraBuildInfo.sourceDirty === null ? "unknown" : vectoraBuildInfo.sourceDirty ? "dirty" : "clean"}`,
    );
  }
  void logActualGraphicsInfo("app-ready");
  if (installedTestsMode) {
    const executable = backendPath(
      process.env,
      process.platform,
      _resourcesPath(),
    );
    const testRunner = spawnBackendProcess(executable, ["--tests"], {
      ...process.env,
      VECTORA_TEST_FROM_ELECTRON: "1",
    });
    testRunner.stdout?.on("data", (chunk: Buffer) =>
      process.stdout.write(chunk),
    );
    testRunner.stderr?.on("data", (chunk: Buffer) =>
      process.stderr.write(chunk),
    );
    testRunner.once("error", (error) => {
      console.error("Não foi possível iniciar os testes da instalação", error);
      app.exit(2);
    });
    testRunner.once("close", (code) => app.exit(code ?? 2));
    return;
  }

  registerDeepLinkProtocol();
  registerIpc();
  // Ponte IPC: serve a SPA e encaminha /auth, /vectora.*, /mcp, SSE… ao backend
  // pelo unix socket (Linux/macOS) ou TCP loopback (Windows).
  protocol.handle(APP_SCHEME, (req) => forwardToBackend(req));

  // Restaura a sessão persistida (arquivo local, ver persistCookieStore) do
  // boot anterior para o _cookieStore in-memory. Sem isso a sessão de login
  // é perdida a cada reinicialização do app.
  loadPersistedCookieStore();

  try {
    await killStaleBackend();
    await startBackend();
    await waitForBackend();
    backendReady = true;
    emitSmokeStage("backend_ready");
    await clearPendingUpdate();
    const webMode = process.env.VECTORA_WEB_MODE === "1";
    // In `vectora web`, keep a hidden renderer alive so BrowserViews/CDP use
    // the bundled Chromium, but leave the visible UI to the user's browser.
    createWindow({ visible: !webMode });
    if (!webMode) createTray();
    setupAutoUpdater();
    if (app.isPackaged && (await isAutoUpdateEnabled())) {
      scheduleAutoUpdateChecks();
    }
  } catch (err) {
    if (await rollbackPendingUpdate()) {
      requestElectronRestart();
      return;
    }
    dialog.showErrorBox(
      "Vectora",
      `Falha ao iniciar: ${(err as Error).message}`,
    );
    app.exit(1);
  }
});

app.on("window-all-closed", () => {
  // Mantém app vivo no tray em todas plataformas — usuário usa "Sair" no
  // menu ou Cmd+Q (macOS).
  if (process.platform !== "darwin") {
    // `vectora web` usa o Electron apenas como sidecar oculto para Chromium,
    // Node e CDP. Não há tray nem janela visível nesse modo, portanto fechar
    // a janela oculta não pode encerrar o backend web.
    if (process.env.VECTORA_WEB_MODE === "1") return;
    // No Windows/Linux, sem tray seria zumbi; com tray, fica disponível.
    if (!tray) app.quit();
  }
});

app.on("before-quit", (event) => {
  (app as unknown as { isQuitting: boolean }).isQuitting = true;
  if (!shutdownResourcesDisposed) {
    shutdownResourcesDisposed = true;
    dmrOperations?.dispose();
    electronCdpProxy?.close();
    electronCdpProxy = null;
  }

  if (backendShutdownPromise) {
    event.preventDefault();
    return;
  }

  if (backend?.pid) {
    event.preventDefault();
    const backendToStop = backend;
    backend = null;
    backendShutdownPromise = Promise.resolve(backendPidFileWrite)
      .then(() =>
        stopBackendProcess(backendToStop, process.platform, signalProcessTree),
      )
      .then((stopped) => {
        if (!stopped) {
          console.error(
            `[backend] processo pid=${backendToStop.pid} ainda vive após SIGKILL`,
          );
        }
      })
      .catch((error: unknown) => {
        console.error(
          "[backend] falha ao encerrar a árvore do processo",
          error,
        );
      })
      .finally(() => {
        backendShutdownPromise = null;
        try {
          fs.unlinkSync(_BACKEND_PID_FILE);
        } catch {}
        app.quit();
      });
    return;
  }

  try {
    fs.unlinkSync(_BACKEND_PID_FILE);
  } catch {}
});

app.on("activate", () => {
  // macOS: clicar no dock recria janela se fechada.
  if (BrowserWindow.getAllWindows().length === 0 && backendReady) {
    createWindow();
  }
});
