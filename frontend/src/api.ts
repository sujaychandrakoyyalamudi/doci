let demoRole = "analyst";
let tokenProvider: (() => Promise<string>) | null = null;

export function setRole(role: string) {
  demoRole = role;
}
export function setTokenProvider(provider: (() => Promise<string>) | null) {
  tokenProvider = provider;
}

async function headers(init?: HeadersInit) {
  const result = new Headers(init);
  if (tokenProvider)
    result.set("Authorization", `Bearer ${await tokenProvider()}`);
  else result.set("X-Demo-Role", demoRole);
  return result;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const h = await headers(init.headers);
  if (init.body && !(init.body instanceof FormData))
    h.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...init, headers: h });
  if (!response.ok) {
    const data = await response
      .json()
      .catch(() => ({ detail: "The request could not be completed." }));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Check the form fields and try again.",
    );
  }
  return response.json() as Promise<T>;
}

export async function downloadDocument(id: string, filename: string) {
  const response = await fetch(`/api/documents/${id}/content`, {
    headers: await headers(),
  });
  if (!response.ok) throw new Error("The document could not be downloaded.");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
