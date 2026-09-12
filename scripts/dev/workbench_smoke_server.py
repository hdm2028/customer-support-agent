"""Disposable local UI verification server; no business DB or live model/payment."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="support-ui-smoke-") as tmp:
        sandbox = Path(tmp)
        for folder in ("app", "web", "data/knowledge"):
            shutil.copytree(ROOT / folder, sandbox / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "main.py", sandbox / "main.py")
        shutil.copy2(ROOT / "data/orders.json", sandbox / "data/orders.json")
        env = dict(os.environ, DATABASE_BACKEND="sqlite", DATABASE_PATH=str(sandbox / "data/ui.db"),
                   REDIS_URL="", REDIS_HOST="", MYSQL_DSN="", MYSQL_USER="", MYSQL_DATABASE="",
                   RAG_EMBEDDING_PROVIDER="local", EMBEDDING_DIMENSIONS="256", PAYMENT_ADAPTER="", SEED_DEMO_DATA="true",
                   ZHIPUAI_API_KEY="", ZHIPU_API_KEY="", LLM_API_KEY="", BIGMODEL_API_KEY="",
                   AUTH_TOKENS='{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":{"user_id":"operator","role":"admin"}}',
                   PYTHONPATH=str(sandbox), PYTHONIOENCODING="utf-8")
        bootstrap = '''
import uvicorn
from app.storage.database import init_database,get_order_from_db
from app.rag.index_manager import get_rag_index_manager
from app.core.schemas import RouteDecision
from app.agent.state import AgentState
from app.agent.agents.after_sales import AfterSalesAgent
from app.tools.policy import policy_search
from app.tools.refund import refund_apply
init_database()
get_rag_index_manager().refresh()
refund=refund_apply('10010','申请退款')
state=AgentState(conversation_id='ui-smoke',user_message='申请退款',
    route=RouteDecision(order_id='10010',need_refund_request=True),
    order=get_order_from_db('10010'),refund=refund.result,
    history=[{'role':'user','content':'请审核我的退款申请'}],
    tool_results=[policy_search('退款人工审核','退款人工审核'),refund])
AfterSalesAgent().create_manual_review(state)
uvicorn.run('main:app',host='127.0.0.1',port=8137)
'''
        subprocess.run([sys.executable, "-X", "utf8", "-c", bootstrap], cwd=sandbox, env=env)


if __name__ == "__main__":
    main()
