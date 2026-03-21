import type { AutobotInferenceConfig } from "./config";
import type {
  InferenceRequestCritic,
  InferenceRequestPatcher,
  InferenceRequestPlanner,
} from "./types";

/**
 * Calls your HTTP gateway in front of Vertex AI. Expected shapes (adjust to match your deploy):
 *
 * POST {baseUrl}{path}  JSON body → { "text": "model output" } or raw string.
 */
export class InferenceClient {
  constructor(private readonly cfg: AutobotInferenceConfig) {}

  private url(path: string): string {
    if (!this.cfg.baseUrl) {
      throw new Error(
        "autobot.inference.baseUrl is empty. Set it to your API gateway URL (Vertex behind Cloud Run, etc.)."
      );
    }
    const p = path.startsWith("/") ? path : `/${path}`;
    return `${this.cfg.baseUrl}${p}`;
  }

  private async post(path: string, body: unknown): Promise<string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.cfg.apiKey) {
      headers.Authorization = `Bearer ${this.cfg.apiKey}`;
    }
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), this.cfg.timeoutMs);
    try {
      const res = await fetch(this.url(path), {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Inference ${res.status}: ${text.slice(0, 800)}`);
      }
      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        const data = (await res.json()) as Record<string, unknown>;
        if (typeof data.text === "string") {
          return data.text;
        }
        if (typeof data.output === "string") {
          return data.output;
        }
        if (typeof data.prediction === "string") {
          return data.prediction;
        }
        // Vertex custom prediction sometimes nests predictions
        const preds = data.predictions;
        if (Array.isArray(preds) && typeof preds[0] === "string") {
          return preds[0];
        }
        return JSON.stringify(data);
      }
      return await res.text();
    } finally {
      clearTimeout(t);
    }
  }

  async planner(req: InferenceRequestPlanner): Promise<string> {
    return this.post(this.cfg.plannerPath, req);
  }

  async patcher(req: InferenceRequestPatcher): Promise<string> {
    return this.post(this.cfg.patcherPath, req);
  }

  async critic(req: InferenceRequestCritic): Promise<string> {
    return this.post(this.cfg.criticPath, req);
  }
}
