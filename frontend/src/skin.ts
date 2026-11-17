// Visual skin ("theme") of the UI. Purely cosmetic: it only switches a
// data-skin attribute on <html>; every skin uses the same markup and features.
// The choice is kept per browser (localStorage), so a phone and a desktop can differ.
import { useEffect, useState } from "react";

export type Skin = "classic" | "ink";
export const SKINS: { id: Skin; label: string; hint: string }[] = [
  { id: "classic", label: "Classic", hint: "Warm paper, serif titles, vermilion accent" },
  { id: "ink", label: "Manga Ink", hint: "Bold ink panels, screentone, sakura accent" },
];

const KEY = "mdm-skin";
const EVENT = "mdm-skin-change";

export function getSkin(): Skin {
  try {
    return localStorage.getItem(KEY) === "ink" ? "ink" : "classic";
  } catch {
    return "classic";
  }
}

export function applySkin(skin: Skin): void {
  document.documentElement.dataset.skin = skin;
}

export function setSkin(skin: Skin): void {
  try {
    localStorage.setItem(KEY, skin);
  } catch {
    /* storage blocked: the skin still applies for this page view */
  }
  applySkin(skin);
  window.dispatchEvent(new Event(EVENT));
}

export function useSkin(): [Skin, (s: Skin) => void] {
  const [skin, setState] = useState<Skin>(getSkin);
  useEffect(() => {
    const sync = () => {
      const s = getSkin();
      applySkin(s);
      setState(s);
    };
    window.addEventListener(EVENT, sync);
    window.addEventListener("storage", sync); // other tabs
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return [skin, setSkin];
}
