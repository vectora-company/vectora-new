/**
 * Tamanho default da janela principal — extraído de main.ts pra ser
 * testável sem importar `electron` (só existe dentro do processo main
 * real). Recebe a área útil da tela como parâmetro explícito em vez de
 * chamar `screen.getPrimaryDisplay()` diretamente.
 */

export interface WindowSize {
  width: number;
  height: number;
}

// A tile no Niri pode ficar bem menor que a janela de desktop tradicional.
// BrowserWindow e screen usam DIP no Electron; não multiplicar pelo scaleFactor.
const MIN_WINDOW_SIZE: WindowSize = { width: 320, height: 240 };

/**
 * ~62% da área útil da tela, com limites padrão de 960x600 e 1280x720.
 * O tamanho final nunca excede a área disponível, inclusive em monitores
 * menores do que o mínimo padrão.
 */
export function computeDefaultWindowSize(screenSize: WindowSize): WindowSize {
  return {
    width: Math.round(
      Math.min(
        screenSize.width,
        Math.min(Math.max(screenSize.width * 0.62, 960), 1280),
      ),
    ),
    height: Math.round(
      Math.min(
        screenSize.height,
        Math.min(Math.max(screenSize.height * 0.62, 600), 720),
      ),
    ),
  };
}

/** Minimum native bounds stay usable while allowing narrow compositor tiles. */
export function computeMinimumWindowSize(screenSize: WindowSize): WindowSize {
  return {
    width: Math.min(MIN_WINDOW_SIZE.width, screenSize.width),
    height: Math.min(MIN_WINDOW_SIZE.height, screenSize.height),
  };
}

/**
 * Wayland deixa o compositor controlar posição e tamanho da superfície.
 * Reaplicar bounds após cada resize disputa com o configure enviado pelo Niri
 * e pode prender a janela no tamanho anterior. O ajuste manual fica para X11.
 */
export function shouldFitWindowToWorkArea(
  platform: string,
  environment: NodeJS.ProcessEnv,
): boolean {
  if (platform !== "linux") return false;
  return (
    environment.XDG_SESSION_TYPE !== "wayland" && !environment.WAYLAND_DISPLAY
  );
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Bounds da janela encaixados na área útil da tela. Uma janela sem moldura que o
 * gerenciador de janelas maximiza pode sobrar além da tela (os botões de fechar
 * ficam para fora); devolve `null` quando já cabe.
 */
export function fitBoundsToWorkArea(bounds: Rect, workArea: Rect): Rect | null {
  const width = Math.min(bounds.width, workArea.width);
  const height = Math.min(bounds.height, workArea.height);
  const x = Math.min(
    Math.max(bounds.x, workArea.x),
    workArea.x + workArea.width - width,
  );
  const y = Math.min(
    Math.max(bounds.y, workArea.y),
    workArea.y + workArea.height - height,
  );
  const fits =
    width === bounds.width &&
    height === bounds.height &&
    x === bounds.x &&
    y === bounds.y;
  return fits ? null : { x, y, width, height };
}
