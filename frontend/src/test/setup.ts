// Vitest setup: DOM matchers (toBeInTheDocument, toHaveTextContent, ...) and a clean DOM per test.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());
