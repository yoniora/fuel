from fastapi import FastAPI
from pydantic import BaseModel
from fuel_app import run_optimizer   # <-- we'll create this function

app = FastAPI()

class OptimiseRequest(BaseModel):
    origin: str
    destination: str
    fuel_type: str
    litres: float

@app.post("/optimise")
def optimise(req: OptimiseRequest):
    result = run_optimizer(
        origin=req.origin,
        destination=req.destination,
        fuel_type=req.fuel_type,
        litres=req.litres
    )
    return result
