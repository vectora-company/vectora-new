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

// Mínimo desejado na superfície lógica do compositor. Em Wayland com escala
// fracionária, o limite nativo do BrowserWindow vira área de superfície vezes
// o scaleFactor; computeMinimumWindowSize converte o valor para esse espaço.
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
export function computeMinimumWindowSize(
  screenSize: WindowSize,
  scaleFactor = 1,
): WindowSize {
  const safeScaleFactor =
    Number.isFinite(scaleFactor) && scaleFactor > 0 ? scaleFactor : 1;
  return {
    width: Math.min(
      Math.floor(MIN_WINDOW_SIZE.width / safeScaleFactor),
      screenSize.width,
    ),
    height: Math.min(
      Math.floor(MIN_WINDOW_SIZE.height / safeScaleFactor),
      screenSize.height,
    ),
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

/** Atualiza o mínimo nativo quando o resize cruza telas com áreas/escala distintas. */
export function updateWindowMinimumSize(
  screenSize: WindowSize,
  scaleFactor: number,
  currentMinimum: WindowSize,
  setMinimumSize: (size: WindowSize) => void,
): boolean {
  const minimum = computeMinimumWindowSize(screenSize, scaleFactor);
  if (
    currentMinimum.width !== minimum.width ||
    currentMinimum.height !== minimum.height
  ) {
    setMinimumSize(minimum);
    return true;
  }
  return false;
}
