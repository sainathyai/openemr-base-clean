"""HTTP serving layer (D-3) for the Clinical Co-Pilot.

A thin FastAPI app that wraps the already-built agents and serves the single-page
UI as static files from the same process. No new clinical logic lives here: it
calls agent.run (UC-1), get_chat (UC-3), and order-safety (UC-2), and shapes their
typed results into JSON. One uvicorn worker serves both API and UI, which is what
keeps the deployed footprint inside the t3.micro memory budget.
"""
