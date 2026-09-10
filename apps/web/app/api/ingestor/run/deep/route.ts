import { operatorProxy } from "../../../../operator-proxy";

export async function POST(request: Request) {
  return operatorProxy(request, "/ingestor/run/deep", "POST");
}
