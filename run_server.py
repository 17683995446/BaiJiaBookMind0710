"""入口：python run_server.py"""
import uvicorn
if __name__ == "__main__":
    uvicorn.run("book_digest.server:app", host="0.0.0.0", port=8000, reload=False)
