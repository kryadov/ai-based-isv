// ts-greeter: a tiny HTTP service (node:http, no framework).
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { greet } from "./greet.js";

export function handle(req: IncomingMessage, res: ServerResponse): void {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/healthz") {
    send(res, 200, { status: "ok" });
  } else if (url.pathname === "/api/greet") {
    send(res, 200, { greeting: greet(url.searchParams.get("name") ?? "") });
  } else {
    send(res, 404, { error: "not found" });
  }
}

function send(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

if (process.argv[1]?.endsWith("server.js")) {
  createServer(handle).listen(Number(process.env.PORT ?? 8000));
}
