import { describe, it, expect, vi } from "vitest";
import {
  computeDefaultWindowSize,
  computeMinimumWindowSize,
  fitBoundsToWorkArea,
  shouldFitWindowToWorkArea,
  updateWindowMinimumSize,
} from "../window-size";

describe("computeDefaultWindowSize", () => {
  it("escala pra ~62% de uma tela 1080p, bem menor que a tela cheia", () => {
    const size = computeDefaultWindowSize({ width: 1920, height: 1080 });
    expect(size).toEqual({ width: 1190, height: 670 });
    expect(size.width).toBeLessThan(1920 * 0.7);
    expect(size.height).toBeLessThan(1080 * 0.7);
  });

  it("capa no mínimo usável numa tela pequena e no máximo numa tela grande (par de borda)", () => {
    const small = computeDefaultWindowSize({ width: 1024, height: 768 });
    expect(small).toEqual({ width: 960, height: 600 });

    const huge = computeDefaultWindowSize({ width: 3840, height: 2160 });
    expect(huge).toEqual({ width: 1280, height: 720 });
  });

  it("respeita o tamanho da área de trabalho em monitores menores que o default", () => {
    expect(computeDefaultWindowSize({ width: 500, height: 400 })).toEqual({
      width: 500,
      height: 400,
    });
  });
});

describe("computeMinimumWindowSize", () => {
  it("permite reduzir a janela abaixo do default em tiles estreitos", () => {
    expect(computeMinimumWindowSize({ width: 1920, height: 1080 })).toEqual({
      width: 320,
      height: 240,
    });
  });

  it("não define um mínimo maior que a área disponível", () => {
    expect(computeMinimumWindowSize({ width: 240, height: 180 })).toEqual({
      width: 240,
      height: 180,
    });
  });

  it("converte o mínimo lógico para escalas fracionárias e inteiras", () => {
    expect(
      computeMinimumWindowSize({ width: 1920, height: 1080 }, 1.5),
    ).toEqual({ width: 213, height: 160 });
    expect(computeMinimumWindowSize({ width: 1920, height: 1080 }, 2)).toEqual({
      width: 160,
      height: 120,
    });
  });

  it("usa escala padrão quando o fator recebido não é válido", () => {
    expect(
      computeMinimumWindowSize({ width: 1920, height: 1080 }, Number.NaN),
    ).toEqual({ width: 320, height: 240 });
  });
});

describe("shouldFitWindowToWorkArea", () => {
  it("deixa o Niri e outros compositores Wayland controlarem o tamanho", () => {
    expect(
      shouldFitWindowToWorkArea("linux", {
        XDG_SESSION_TYPE: "wayland",
        WAYLAND_DISPLAY: "wayland-1",
      }),
    ).toBe(false);
    expect(
      shouldFitWindowToWorkArea("linux", { WAYLAND_DISPLAY: "wayland-1" }),
    ).toBe(false);
  });

  it("mantém o ajuste de workarea no X11 e não tenta no macOS/Windows", () => {
    expect(
      shouldFitWindowToWorkArea("linux", { XDG_SESSION_TYPE: "x11" }),
    ).toBe(true);
    expect(shouldFitWindowToWorkArea("darwin", {})).toBe(false);
    expect(shouldFitWindowToWorkArea("win32", {})).toBe(false);
  });
});

describe("fitBoundsToWorkArea", () => {
  const workArea = { x: 0, y: 0, width: 1920, height: 1040 };

  it("não mexe numa janela que já cabe", () => {
    expect(
      fitBoundsToWorkArea({ x: 0, y: 0, width: 1920, height: 1040 }, workArea),
    ).toBeNull();
    expect(
      fitBoundsToWorkArea({ x: 100, y: 80, width: 800, height: 600 }, workArea),
    ).toBeNull();
  });

  it("encolhe uma janela maximizada que sobrou além da tela (botão de fechar fora)", () => {
    expect(
      fitBoundsToWorkArea({ x: 0, y: 0, width: 1956, height: 1040 }, workArea),
    ).toEqual({ x: 0, y: 0, width: 1920, height: 1040 });
  });

  it("puxa de volta uma janela que ficou deslocada para a direita ou para baixo", () => {
    expect(
      fitBoundsToWorkArea(
        { x: 40, y: 30, width: 1920, height: 1040 },
        workArea,
      ),
    ).toEqual({ x: 0, y: 0, width: 1920, height: 1040 });
  });

  it("respeita a origem da área útil em monitores deslocados", () => {
    const second = { x: 1920, y: 24, width: 1366, height: 744 };
    expect(
      fitBoundsToWorkArea({ x: 1900, y: 0, width: 1400, height: 800 }, second),
    ).toEqual({ x: 1920, y: 24, width: 1366, height: 744 });
  });

  it("permite o compositor reduzir e ampliar a janela sem reaplicar bounds", () => {
    const logicalWorkArea = { x: 0, y: 0, width: 1280, height: 800 };
    const narrowTile = { x: 0, y: 0, width: 420, height: 360 };
    const widerTile = { x: 0, y: 0, width: 860, height: 600 };

    expect(fitBoundsToWorkArea(narrowTile, logicalWorkArea)).toBeNull();
    expect(fitBoundsToWorkArea(widerTile, logicalWorkArea)).toBeNull();
    expect(fitBoundsToWorkArea(narrowTile, logicalWorkArea)).toBeNull();
  });

  it.each([1, 1.25, 1.5, 2])(
    "atualiza o mínimo e preserva o ciclo de resize Wayland em scaleFactor %s",
    (scaleFactor) => {
      const physicalWorkArea = { width: 2560, height: 1600 };
      const scaledWorkArea = {
        x: 0,
        y: 0,
        width: Math.round(physicalWorkArea.width / scaleFactor),
        height: Math.round(physicalWorkArea.height / scaleFactor),
      };
      const toLogical = (value: number) => Math.round(value / scaleFactor);
      const compositorBounds = [
        { x: 0, y: 0, width: 630, height: 540 },
        { x: 0, y: 0, width: 1290, height: 900 },
        { x: 0, y: 0, width: 630, height: 540 },
      ].map((rect) => ({
        x: rect.x,
        y: rect.y,
        width: toLogical(rect.width),
        height: toLogical(rect.height),
      }));
      const setMinimumSize = vi.fn();
      let currentMinimum = computeMinimumWindowSize(
        scaledWorkArea,
        scaleFactor,
      );

      for (const bounds of compositorBounds) {
        expect(fitBoundsToWorkArea(bounds, scaledWorkArea)).toBeNull();
        expect(
          updateWindowMinimumSize(
            scaledWorkArea,
            scaleFactor,
            currentMinimum,
            (minimum) => {
              setMinimumSize(minimum);
              currentMinimum = minimum;
            },
          ),
        ).toBe(false);
      }

      expect(setMinimumSize).not.toHaveBeenCalled();
      expect(compositorBounds[0]?.width).toBe(compositorBounds[2]?.width);
      expect(compositorBounds[0]?.height).toBe(compositorBounds[2]?.height);
      expect(compositorBounds[1]?.width).toBeGreaterThan(
        compositorBounds[0]?.width ?? 0,
      );
      expect(compositorBounds[1]?.height).toBeGreaterThan(
        compositorBounds[0]?.height ?? 0,
      );
    },
  );

  it("continua corrigindo bounds fora da tela no X11", () => {
    const fitted = fitBoundsToWorkArea(
      { x: 0, y: 0, width: 1956, height: 1040 },
      workArea,
    );
    expect(fitted).toEqual({
      x: 0,
      y: 0,
      width: 1920,
      height: 1040,
    });
  });

  it("recalcula o mínimo ao mover entre monitores com escalas diferentes", () => {
    const setMinimumSize = vi.fn();
    expect(
      updateWindowMinimumSize(
        { width: 1920, height: 1080 },
        1,
        { width: 213, height: 160 },
        setMinimumSize,
      ),
    ).toBe(true);
    expect(setMinimumSize).toHaveBeenCalledWith({ width: 320, height: 240 });
    expect(
      updateWindowMinimumSize(
        { width: 1920, height: 1080 },
        1,
        { width: 320, height: 240 },
        setMinimumSize,
      ),
    ).toBe(false);
    expect(setMinimumSize).toHaveBeenCalledTimes(1);
    expect(
      updateWindowMinimumSize(
        { width: 1920, height: 1080 },
        2,
        { width: 320, height: 240 },
        setMinimumSize,
      ),
    ).toBe(true);
    expect(setMinimumSize).toHaveBeenLastCalledWith({
      width: 160,
      height: 120,
    });
  });
});
