/**
 * Ciclo de vida do backend sidecar — spawn, resolução de path, health-check
 * com backoff e encerramento — extraído de main.ts pra ser testável sem
 * importar `electron` (que só existe dentro do processo main do Electron
 * de verdade). Todo estado que main.ts guardava em variáveis de módulo
 * (backendPort, backendPipePath, o processo em si) passa a ser parâmetro
 * explícito das funções aqui, permitindo testar cada peça isoladamente
 * contra um processo Node real (ver __tests__/backend-lifecycle.test.ts).
 */

import { spawn, spawnSync, ChildProcess } from "child_process";
import * as http from "http";
import * as net from "net";
import * as path from "path";
import type { Writable } from "stream";

export function ignoreBrokenPipe(stream: Writable): void {
  stream.on("error", (error: Error) => {
    if ((error as NodeJS.ErrnoException).code !== "EPIPE") throw error;
  });
}

// ---------------------------------------------------------------------------
// Porta livre
// ---------------------------------------------------------------------------

export function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      if (address && typeof address === "object") {
        const port = address.port;
        srv.close(() => resolve(port));
      } else {
        srv.close();
        reject(new Error("Sem porta livre."));
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Resolução de path — funções puras, sem I/O além do fs.existsSync opcional
// ---------------------------------------------------------------------------

export interface PathResolutionEnv {
  VECTORA_CORE_PATH?: string;
  VECTORA_DEV_BACKEND?: string;
  VECTORA_DEV_BACKEND_EXECUTABLE?: string;
  VECTORA_NATS_BINARY?: string;
}

export interface BackendLaunch {
  executable: string;
  args: string[];
  cwd?: string;
}

/**
 * Resolve o path do binário do backend (PyInstaller). Prod: `resourcesPath/vectora-core/`
 * (electron-builder `extraResources`). Dev: override por
 * `env.VECTORA_CORE_PATH` apontando pra um build local.
 */
export function backendPath(
  env: PathResolutionEnv,
  platform: NodeJS.Platform,
  resourcesPath: string,
): string {
  const override = env.VECTORA_CORE_PATH;
  if (override) {
    return path.join(
      override,
      platform === "win32" ? "vectora.exe" : "vectora",
    );
  }
  const exe = platform === "win32" ? "vectora.exe" : "vectora";
  return path.join(resourcesPath, "vectora-core", exe);
}

/** Resolve o backend que o Electron deve iniciar no modo alternativo de dev. */
export function backendLaunch(
  env: PathResolutionEnv,
  platform: NodeJS.Platform,
  resourcesPath: string,
  sourceRoot: string,
): BackendLaunch {
  if (env.VECTORA_DEV_BACKEND === "source") {
    return {
      executable:
        env.VECTORA_DEV_BACKEND_EXECUTABLE ??
        (platform === "win32" ? "uv.exe" : "uv"),
      args: ["run", "vectora", "start"],
      cwd: sourceRoot,
    };
  }
  return {
    executable: backendPath(env, platform, resourcesPath),
    args: ["start"],
  };
}

/**
 * Resolução de conexão pro modo backend-primário em dev: quando o backend
 * Python já é o processo primário e nos spawnou (invertendo a direção usual
 * Electron→backend), ele sinaliza isso via `VECTORA_EXTERNAL_BACKEND=1` e já
 * conhece porta/pipe de antemão — retorna `null` no caminho normal
 * (produção, Electron spawna o backend) pra `startBackend()` seguir seu
 * fluxo de sempre.
 */
export interface ExternalBackendConnection {
  port: number | null;
  pipePath: string | null;
  webMode: boolean;
}

/** Selects the desktop IPC transport or the externally exposed web transport. */
export function backendTransportOptions(
  platform: NodeJS.Platform,
  runtimeHome: string,
  external: ExternalBackendConnection | null,
): http.RequestOptions {
  if (platform === "win32") {
    if (external?.pipePath) return { socketPath: external.pipePath };
    return { host: "127.0.0.1", port: external?.port ?? undefined };
  }
  if (external) {
    if (external.webMode) {
      return { host: "127.0.0.1", port: external.port ?? undefined };
    }
    return { socketPath: path.join(runtimeHome, "vectora.sock") };
  }
  return { socketPath: path.join(runtimeHome, "vectora.sock") };
}

export function resolveExternalBackendConnection(env: {
  VECTORA_EXTERNAL_BACKEND?: string;
  VECTORA_PORT?: string;
  VECTORA_IPC_PIPE?: string;
  VECTORA_WEB_MODE?: string;
}): ExternalBackendConnection | null {
  if (env.VECTORA_EXTERNAL_BACKEND !== "1") return null;
  return {
    port: Number(env.VECTORA_PORT) || null,
    pipePath: env.VECTORA_IPC_PIPE ?? null,
    webMode: env.VECTORA_WEB_MODE === "1",
  };
}

/**
 * Resolve o binário nats-server empacotado (extraResources → resources/).
 * `existsFn` é injetável só pra testes puros sem tocar o filesystem real
 * (produção sempre passa `fs.existsSync`).
 */
export function natsBinaryPath(
  env: PathResolutionEnv,
  platform: NodeJS.Platform,
  resourcesPath: string,
  existsFn: (p: string) => boolean,
): string | null {
  if (env.VECTORA_NATS_BINARY) return env.VECTORA_NATS_BINARY;
  const exe = platform === "win32" ? "nats-server.exe" : "nats-server";
  const p = path.join(resourcesPath, exe);
  return existsFn(p) ? p : null;
}

// ---------------------------------------------------------------------------
// Parsing do handshake VECTORA_IPC_PIPE= no stdout
// ---------------------------------------------------------------------------

/**
 * Extrai `VECTORA_IPC_PIPE=<path>` de um chunk de texto de stdout, se
 * presente. Retorna `null` quando o chunk não contém o marcador — inclusive
 * quando ele está fatiado entre dois chunks (kernel write parcial); nesse
 * caso o chamador precisa acumular chunks antes de tentar o parse de novo
 * (ver `IpcPipeParser` abaixo, que faz exatamente isso).
 */
export function parseIpcPipeFromText(text: string): string | null {
  const match = /VECTORA_IPC_PIPE=(.+)/.exec(text);
  return match ? match[1].trim() : null;
}

/**
 * Acumula chunks de stdout até fechar uma linha completa antes de tentar
 * extrair o marcador `VECTORA_IPC_PIPE=` — protege contra o write parcial
 * do kernel entregar `VECTORA_IPC_PIPE=\\.\pi` num chunk e `pe\test\n` no
 * seguinte, caso em que um regex por-chunk isolado (sem acumular) nunca
 * casaria com nenhum dos dois pedaços.
 */
export class IpcPipeParser {
  private buffer = "";
  private found: string | null = null;

  /** Alimenta mais um chunk; retorna o pipe path assim que uma linha
   * completa contendo o marcador for vista (idempotente depois disso).
   *
   * Só tenta o parse quando uma quebra de linha (`\n`) de fato chegou —
   * sem isso, `.+` do regex casa greedily contra o buffer AINDA
   * incompleto (ex.: "VECTORA_IPC_PIPE=\\.\pipe\vectora-" sem o resto)
   * e devolve um pipe path truncado, quebrando o transporte silenciosamente.
   */
  push(chunk: string): string | null {
    if (this.found) return this.found;
    this.buffer += chunk;

    const newlineIdx = this.buffer.indexOf("\n");
    if (newlineIdx === -1) return null; // linha ainda incompleta

    const line = this.buffer.slice(0, newlineIdx);
    this.buffer = this.buffer.slice(newlineIdx + 1);

    const match = parseIpcPipeFromText(line);
    if (match) {
      this.found = match;
      return match;
    }
    // Linha completa sem o marcador — pode haver outra linha completa já
    // no buffer (múltiplas linhas chegaram no mesmo chunk); tenta de novo.
    return this.buffer.includes("\n") ? this.push("") : null;
  }
}

// ---------------------------------------------------------------------------
// Spawn
// ---------------------------------------------------------------------------

export function isInstalledTestsMode(argv: string[]): boolean {
  return argv.includes("--tests");
}

export function isInstalledSmokeTestMode(argv: string[]): boolean {
  return argv.includes("--smoke-test");
}

export function isInstalledUiSmokeTestMode(argv: string[]): boolean {
  return argv.includes("--ui-smoke-test");
}

export function spawnBackendProcess(
  exePath: string,
  args: string[],
  env: NodeJS.ProcessEnv,
  cwd?: string,
): ChildProcess {
  return spawn(exePath, args, {
    env,
    ...(cwd ? { cwd } : {}),
    stdio: ["ignore", "pipe", "pipe"],
  });
}

// ---------------------------------------------------------------------------
// Health-check com backoff exponencial
// ---------------------------------------------------------------------------

export function pingBackendHttp(
  transport: http.RequestOptions,
  urlPath: string,
): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      { ...transport, method: "GET", path: urlPath },
      (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 400);
      },
    );
    req.on("error", () => resolve(false));
    req.end();
  });
}

export function signLaunchHandshake(
  transport: http.RequestOptions,
  mode: "python-primary" | "electron-primary",
  token: string,
): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        ...transport,
        method: "POST",
        path: "/internal/desktop/handshake",
        headers: {
          "x-vectora-launch-mode": mode,
          "x-vectora-launch-token": token,
        },
      },
      (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 400);
      },
    );
    req.on("error", () => resolve(false));
    req.end();
  });
}

export function unixSocketIsActive(
  socketPath: string,
  timeoutMs = 1_000,
): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection(socketPath);
    let settled = false;
    let response = "";
    let timeout: NodeJS.Timeout;
    const finish = (active: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      socket.destroy();
      resolve(active);
    };
    timeout = setTimeout(() => finish(false), timeoutMs);
    socket.once("connect", () =>
      socket.write(
        "GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
      ),
    );
    socket.on("data", (chunk: Buffer) => {
      response += chunk.toString("utf-8");
      if (response.includes("\r\n")) finish(response.startsWith("HTTP/"));
    });
    socket.once("error", () => finish(false));
  });
}

/**
 * GET JSON de um endpoint do backend pelo transporte IPC — usado pra ler
 * settings antes de decidir comportamento do main process (ex.: gate de
 * auto-update em GET /settings/prefs). `null` em qualquer falha (rede,
 * status >= 400, JSON inválido) — o chamador decide o default nesse caso.
 */
/** Fetch a JSON response from the local backend with optional auth headers. */
export function fetchBackendJson<T = unknown>(
  transport: http.RequestOptions,
  urlPath: string,
  headers: Record<string, string> = {},
): Promise<T | null> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        ...transport,
        method: "GET",
        path: urlPath,
        ...(Object.keys(headers).length > 0 ? { headers } : {}),
      },
      (res) => {
        if ((res.statusCode ?? 500) >= 400) {
          res.resume();
          resolve(null);
          return;
        }
        let body = "";
        res.on("data", (chunk: Buffer) => {
          body += chunk;
        });
        res.on("end", () => {
          try {
            resolve(JSON.parse(body) as T);
          } catch {
            resolve(null);
          }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.end();
  });
}

/** POST JSON autenticado pela ponte local do processo principal. */
export function postBackendJson<T = unknown>(
  transport: http.RequestOptions,
  urlPath: string,
  payload: unknown,
  headers: Record<string, string> = {},
): Promise<T | null> {
  return new Promise((resolve) => {
    const body = JSON.stringify(payload);
    const req = http.request(
      {
        ...transport,
        method: "POST",
        path: urlPath,
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body),
          ...headers,
        },
      },
      (res) => {
        if ((res.statusCode ?? 500) >= 400) {
          res.resume();
          resolve(null);
          return;
        }
        let response = "";
        res.on("data", (chunk: Buffer) => {
          response += chunk;
        });
        res.on("end", () => {
          try {
            resolve(JSON.parse(response) as T);
          } catch {
            resolve(null);
          }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.write(body);
    req.end();
  });
}

export interface WaitForBackendOptions {
  ping: () => Promise<boolean>;
  isExited: () => { exited: boolean; code: number | null };
  timeoutMs: number;
  baseDelayMs: number;
  maxDelayMs: number;
  getRecentLogs: () => string;
  sleep?: (ms: number) => Promise<void>;
}

/**
 * Health-check com backoff exponencial. Cada falha dobra o delay até
 * `maxDelayMs`, capado em `timeoutMs` no total. Falha imediatamente se o
 * processo já terminou (crash no startup), sem esperar o timeout inteiro.
 */
export async function waitForBackendReady(
  opts: WaitForBackendOptions,
): Promise<void> {
  const sleep =
    opts.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const deadline = Date.now() + opts.timeoutMs;
  let delay = opts.baseDelayMs;
  while (Date.now() < deadline) {
    const exitState = opts.isExited();
    if (exitState.exited) {
      const logs = opts.getRecentLogs();
      throw new Error(
        `Backend encerrou inesperadamente (code=${exitState.code}).` +
          (logs ? `\n\nÚltimos logs:\n${logs}` : ""),
      );
    }
    if (await opts.ping()) return;
    await sleep(delay);
    delay = Math.min(delay * 2, opts.maxDelayMs);
  }
  const logs = opts.getRecentLogs();
  throw new Error(
    `Backend não respondeu em ${opts.timeoutMs / 1000}s.` +
      (logs ? `\n\nÚltimos logs:\n${logs}` : ""),
  );
}

// ---------------------------------------------------------------------------
// Encerramento
// ---------------------------------------------------------------------------

/**
 * Sinaliza a árvore de processos do PID informado. No Windows, `taskkill`
 * encerra a árvore; `/F` só é usado na escalada para SIGKILL. Nas demais
 * plataformas, `tree-kill` propaga o sinal aos descendentes.
 */
type TreeKillFunction = (
  pid: number,
  signal: string,
  callback?: (error?: Error) => void,
) => void;

export function killBackendTree(
  pid: number,
  platform: NodeJS.Platform,
  treeKillFn: TreeKillFunction,
  signal: NodeJS.Signals = "SIGTERM",
): Promise<void> {
  if (platform === "win32") {
    spawnSync(
      "taskkill",
      ["/T", ...(signal === "SIGKILL" ? ["/F"] : []), "/PID", String(pid)],
      {
        stdio: "ignore",
      },
    );
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    let settled = false;
    let timeout: NodeJS.Timeout;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve();
    };
    // A callback do tree-kill pode não chegar se algum descendente já tiver
    // desaparecido. A confirmação de saída é feita pelo chamador; não deixe
    // o encerramento do app preso esperando um callback perdido.
    timeout = setTimeout(finish, 1_000);
    try {
      treeKillFn(pid, signal, finish);
    } catch {
      // Processo já encerrado ou inacessível: o chamador confirma a saída.
      finish();
    }
  });
}

function waitForChildExit(
  child: ChildProcess,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve(true);
  }

  return new Promise((resolve) => {
    const finish = (exited: boolean) => {
      clearTimeout(timeout);
      child.off("exit", onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    const timeout = setTimeout(() => finish(false), timeoutMs);
    child.once("exit", onExit);
    if (child.exitCode !== null || child.signalCode !== null) finish(true);
  });
}

/** Sends TERM, waits for the backend to exit, then escalates to KILL. */
export async function stopBackendProcess(
  child: ChildProcess,
  platform: NodeJS.Platform,
  treeKillFn: TreeKillFunction,
  gracefulTimeoutMs = 8_000,
  forcedTimeoutMs = 3_000,
): Promise<boolean> {
  const pid = child.pid;
  if (!pid || child.exitCode !== null || child.signalCode !== null) return true;

  await killBackendTree(pid, platform, treeKillFn, "SIGTERM");
  if (await waitForChildExit(child, gracefulTimeoutMs)) return true;

  await killBackendTree(pid, platform, treeKillFn, "SIGKILL");
  return waitForChildExit(child, forcedTimeoutMs);
}

function waitForPidExit(
  pid: number,
  isAlive: (pid: number) => boolean,
  timeoutMs: number,
): Promise<boolean> {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const poll = () => {
      if (!isAlive(pid)) return resolve(true);
      if (Date.now() >= deadline) return resolve(false);
      setTimeout(poll, 100);
    };
    poll();
  });
}

/** Waits for a stale backend PID to exit and escalates if it ignores TERM. */
export async function stopBackendTree(
  pid: number,
  platform: NodeJS.Platform,
  treeKillFn: TreeKillFunction,
  isAlive: (pid: number) => boolean,
  gracefulTimeoutMs = 8_000,
  forcedTimeoutMs = 3_000,
): Promise<boolean> {
  if (!isAlive(pid)) return true;
  await killBackendTree(pid, platform, treeKillFn, "SIGTERM");
  if (await waitForPidExit(pid, isAlive, gracefulTimeoutMs)) return true;
  await killBackendTree(pid, platform, treeKillFn, "SIGKILL");
  return waitForPidExit(pid, isAlive, forcedTimeoutMs);
}
