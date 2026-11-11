"""Run the unified LangChain online entry locally; existing app remains available."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8082)
    parser.add_argument('--config')
    parser.add_argument('--experiment')
    args=parser.parse_args()
    from src.agent.config import load_config
    from src.api.chat_routes import create_app
    from src.agent.runtime import configure_logging
    import uvicorn
    cfg=load_config(args.config,args.experiment)
    configure_logging()
    uvicorn.run(create_app(cfg),host='127.0.0.1',port=args.port,access_log=False)


if __name__=='__main__':main()
