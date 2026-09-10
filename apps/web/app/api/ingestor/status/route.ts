import { operatorProxy } from "../../../operator-proxy";

export async function GET(request: Request) {
  return operatorProxy(request, "/ingestor/status", "GET");
}
