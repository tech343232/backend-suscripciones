from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "ok"}
