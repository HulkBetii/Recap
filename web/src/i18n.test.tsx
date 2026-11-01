import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { LocaleProvider, useLocale } from "./i18n";

function Probe() {
  const { locale, setLocale, t } = useLocale();
  return <div><span>{locale}</span><strong>{t("nav.runs")}</strong><button type="button" onClick={() => setLocale("en")}>English</button></div>;
}

describe("locale provider", () => {
  beforeEach(() => window.localStorage.removeItem("recap.locale"));

  it("defaults to Vietnamese and persists an English choice", () => {
    const view = render(<LocaleProvider><Probe /></LocaleProvider>);
    expect(screen.getByText("vi")).toBeInTheDocument();
    expect(screen.getByText("Các lượt chạy")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "English" }));
    expect(screen.getByText("en")).toBeInTheDocument();
    expect(screen.getByText("Runs")).toBeInTheDocument();
    expect(window.localStorage.getItem("recap.locale")).toBe("en");
    view.unmount();
    render(<LocaleProvider><Probe /></LocaleProvider>);
    expect(screen.getByText("en")).toBeInTheDocument();
    expect(screen.getByText("Runs")).toBeInTheDocument();
  });
});
