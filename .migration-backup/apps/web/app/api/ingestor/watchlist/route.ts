import { operatorProxy } from "../../../operator-proxy";

export async function GET(request: Request) {
  return operatorProxy(request, "/ingestor/watchlist", "GET");
}

export async function POST(request: Request) {
  return operatorProxy(request, "/ingestor/watchlist", "POST");
}
