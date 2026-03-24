from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"ok": True, "message": "Backend funcionando 🚀"}

@app.get("/health")
def health():
    return {"status": "running"}
