/** Typed fetch wrappers for the /api/budgets routes (budget-model-routing.md
 * §3.3).
 *
 * Budgets cap Claude USD spend per window (daily/weekly/monthly), globally or
 * per agent. The enforcement gate lives server-side in the session manager;
 * these routes are only configuration (CRUD) and observation (`/status`, which
 * resolves each enabled budget against live spend so the UI can draw a water
 * level). */

import type {
  BudgetRead,
  BudgetStatusEntry,
  CreateBudgetRequest,
  UpdateBudgetRequest,
} from ".";

const API = window.location.origin;

function authHeaders(token: string): HeadersInit {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function fetchBudgets(token: string): Promise<BudgetRead[]> {
  return json(await fetch(`${API}/api/budgets`, { headers: authHeaders(token) }));
}

export async function fetchBudgetStatus(
  token: string
): Promise<BudgetStatusEntry[]> {
  return json(
    await fetch(`${API}/api/budgets/status`, { headers: authHeaders(token) })
  );
}

export async function createBudget(
  token: string,
  body: CreateBudgetRequest
): Promise<BudgetRead> {
  return json(
    await fetch(`${API}/api/budgets`, {
      method: "POST",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function updateBudget(
  token: string,
  id: string,
  body: UpdateBudgetRequest
): Promise<BudgetRead> {
  return json(
    await fetch(`${API}/api/budgets/${id}`, {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function deleteBudget(token: string, id: string): Promise<void> {
  const res = await fetch(`${API}/api/budgets/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 204) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
}
