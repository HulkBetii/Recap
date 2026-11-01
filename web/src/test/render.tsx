import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { LocaleProvider } from "../i18n";

export function renderUi(element: ReactElement, route = "/runs") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><LocaleProvider><MemoryRouter initialEntries={[route]}>{element}</MemoryRouter></LocaleProvider></QueryClientProvider>);
}
