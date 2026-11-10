"""Probe Tavily: one people search for a bio, if TAVILY_API_KEY is set."""
import json, os, sys
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("TAVILY_API_KEY")
if not key:
    print(json.dumps({"status": "skipped", "reason": "TAVILY_API_KEY not set; bios fall back to the filing's own profile"}))
    sys.exit(0)
from tavily import TavilyClient
r = TavilyClient(api_key=key).search("K Krithivasan TCS CEO professional background", max_results=3)
print(json.dumps({"keys": list(r), "result_keys": list(r["results"][0]) if r.get("results") else [],
                  "sample": [{k: x[k] for k in ("title", "url")} for x in r.get("results", [])]}, indent=1))
