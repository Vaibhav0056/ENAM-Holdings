"""Probe Gemini via google-genai: which generate and embedding models this key can use,
whether structured JSON output works, and whether embeddings honour 768 dimensions."""
import json, os, sys, time
sys.path.insert(0, ".")
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
gen, emb = [], []
for m in client.models.list():
    acts = getattr(m, "supported_actions", None) or []
    name = m.name.replace("models/", "")
    if "generateContent" in acts:
        gen.append(name)
    if "embedContent" in acts:
        emb.append(name)
print("generate models:", len(gen))
print("  ", [g for g in gen if "gemini" in g][:60])
print("embedding models:", emb)


class Probe(BaseModel):
    company: str
    revenue_growth_pct: float


for model in [os.getenv("CHEAP_MODEL"), os.getenv("STRONG_MODEL")] + [g for g in gen if "pro" in g and "gemini" in g][:3]:
    t = time.time()
    try:
        r = client.models.generate_content(
            model=model, contents="Text: 'Polycab revenue grew 18.4% this year.' Extract it.",
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Probe, temperature=0))
        u = r.usage_metadata
        print(f"{model}: ok {round(time.time()-t,1)}s parsed={r.parsed} in={u.prompt_token_count} out={u.candidates_token_count}")
    except Exception as e:
        print(f"{model}: FAIL {str(e)[:160]}")

for model in [os.getenv("EMBED_MODEL", "gemini-embedding-2")] + [e for e in emb if "gemini-embedding" in e][:2]:
    try:
        r = client.models.embed_content(model=model, contents=["Capex guidance of Rs 1,000 crore for FY26"],
                                        config=types.EmbedContentConfig(output_dimensionality=768, task_type="RETRIEVAL_DOCUMENT"))
        print(f"embed {model}: dims={len(r.embeddings[0].values)}")
    except Exception as e:
        print(f"embed {model}: FAIL {str(e)[:160]}")
