import builtins
import io
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st
from groq import Groq

MODEL = "openai/gpt-oss-120b"
MODEL_OPTIONS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
MAX_RETRIES = 2

# patterns I don't want the generated code to be allowed to use
BLOCKED = [
    r"\bimport\b", r"__", r"\bopen\s*\(", r"\bexec\s*\(", r"\beval\s*\(",
    r"\bos\b", r"\bsys\b", r"\bsubprocess\b", r"\bgetattr\b", r"\bsetattr\b",
    r"\bglobals\b", r"\blocals\b", r"\bcompile\b", r"\binput\s*\(",
    r"\.read_\w+", r"\.to_(csv|excel|pickle|sql|json)\b",
]

# only these builtins are available when the code runs
SAFE_NAMES = ["len", "range", "sum", "min", "max", "sorted", "round", "abs",
              "list", "dict", "set", "tuple", "str", "int", "float", "bool",
              "enumerate", "zip", "print", "isinstance", "map", "filter",
              "any", "all", "pow", "divmod", "reversed", "iter", "next",
              "slice", "frozenset", "format", "repr", "ord", "chr"]
SAFE_BUILTINS = {name: getattr(builtins, name) for name in SAFE_NAMES}


def column_list(df):
    return "\n".join(f"- {c}: {t}" for c, t in df.dtypes.items())


def make_prompt(df):
    return f"""You are a Python data analyst.
A pandas DataFrame called df is already loaded. Columns and types:
{column_list(df)}

First 5 rows:
{df.head(5).to_string()}

Rules:
1. Reply with only Python code inside one ```python block, no explanation.
2. pd, np and plt (matplotlib.pyplot) are already imported. Don't import anything.
3. Put the final answer in a variable called result.
4. For charts, draw with plt (start with plt.figure()) and set result to a
   one-line description of the chart.
5. Never read/write files and never use os, sys, open, exec or eval.
6. Use the exact column names given above.
7. If the question refers to an earlier one ("now by month", "same but for
   2011"), build on the previous code shown in the conversation.
"""


def get_code(reply):
    # grab whatever is inside the ```python block
    match = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.DOTALL)
    code = match.group(1).strip() if match else reply.strip()

    # pd/np/plt are already loaded, so drop those import lines if the model adds them
    skip = ("import pandas", "import numpy", "import matplotlib", "from matplotlib")
    lines = [line for line in code.splitlines() if not line.strip().startswith(skip)]
    return "\n".join(lines)


def is_safe(code):
    for pattern in BLOCKED:
        if re.search(pattern, code):
            return False, f"blocked pattern: {pattern}"
    return True, ""


def run_code(code, df):
    plt.close("all")
    env = {"__builtins__": SAFE_BUILTINS, "df": df.copy(),
           "pd": pd, "np": np, "plt": plt}
    exec(code, env)
    fig = plt.gcf() if plt.get_fignums() else None
    return env.get("result"), fig


def fig_to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return buf.getvalue()


def ask_llm(client, messages):
    response = client.chat.completions.create(
        model=st.session_state.get("model", MODEL),
        messages=messages,
        temperature=0,
    )
    return response.choices[0].message.content


def answer_question(client, question, df, history=()):
    messages = [{"role": "system", "content": make_prompt(df)}]

    # replay the last few questions and their code so follow-ups make sense
    for old_question, old_code in list(history)[-3:]:
        messages.append({"role": "user", "content": old_question})
        messages.append({"role": "assistant",
                         "content": f"```python\n{old_code}\n```"})

    messages.append({"role": "user", "content": question})
    code, error = "", ""

    for attempt in range(1, MAX_RETRIES + 2):
        reply = ask_llm(client, messages)
        code = get_code(reply)

        safe, reason = is_safe(code)
        if safe:
            try:
                result, fig = run_code(code, df)
                png = fig_to_png(fig) if fig is not None else None
                return {"code": code, "result": result, "png": png,
                        "attempts": attempt, "error": None}
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
        else:
            error = reason

        # send the error back so the model can fix its own code
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content":
                         f"That code failed with: {error}\n"
                         "Fix it and return only the corrected code."})

    return {"code": code, "result": None, "png": None,
            "attempts": MAX_RETRIES + 1, "error": error}


def explain(client, question, result):
    # one extra LLM call that turns the raw result into a short plain-English note
    if isinstance(result, (pd.DataFrame, pd.Series)):
        text = result.head(20).to_string()
    else:
        text = str(result)

    messages = [
        {"role": "system", "content":
         "You explain data analysis results to a non-technical reader in 2-3 "
         "short sentences. Use only the numbers shown in the result. "
         "Don't invent anything."},
        {"role": "user", "content": f"Question: {question}\n\nResult:\n{text[:2000]}"},
    ]
    try:
        return ask_llm(client, messages)
    except Exception:
        return None  # the explanation is a bonus, so don't break the app


def suggest_questions(client, df):
    prompt = f"""Columns and types:
{column_list(df)}

First 5 rows:
{df.head(5).to_string()}

Suggest 4 interesting questions a business analyst could ask about this data.
Each must be answerable with pandas. Reply with one question per line,
no numbering and no extra text."""
    try:
        reply = ask_llm(client, [{"role": "user", "content": prompt}])
    except Exception:
        return []
    lines = [ln.lstrip("-*• 0123456789.)").strip() for ln in reply.splitlines()]
    return [ln for ln in lines if len(ln) > 10][:4]


def load_data(file):
    if file.name.lower().endswith(".xlsx"):
        return pd.read_excel(file)
    try:
        return pd.read_csv(file)
    except UnicodeDecodeError:
        file.seek(0)
        return pd.read_csv(file, encoding="latin-1")


def show_summary(df):
    col1, col2, col3 = st.columns(3)
    col1.metric("Rows", df.shape[0])
    col2.metric("Columns", df.shape[1])
    col3.metric("Duplicate rows", int(df.duplicated().sum()))

    info = pd.DataFrame({
        "type": df.dtypes.astype(str),
        "missing": df.isna().sum(),
        "missing %": (df.isna().mean() * 100).round(1),
        "unique values": df.nunique(),
    })
    st.write("Column overview")
    st.dataframe(info)

    numeric = df.select_dtypes("number")
    if not numeric.empty:
        st.write("Numeric column statistics")
        st.dataframe(numeric.describe().T)


def show_answer(saved):
    out = saved["out"]
    if out["error"]:
        st.error(f"Couldn't answer after {out['attempts']} attempts. "
                 f"Last error: {out['error']}")
    else:
        st.subheader("Answer")
        result = out["result"]
        if isinstance(result, (pd.DataFrame, pd.Series)):
            st.dataframe(result)
            st.download_button("Download table (CSV)",
                               result.to_csv().encode("utf-8"),
                               "result.csv", "text/csv")
        elif result is not None:
            st.write(result)

        if out["png"]:
            st.image(out["png"])
            st.download_button("Download chart (PNG)", out["png"],
                               "chart.png", "image/png")

        if saved["note"]:
            st.info(saved["note"])
        st.caption(f"Answered in {out['attempts']} attempt(s)")

    with st.expander("Show generated code"):
        st.code(out["code"], language="python")


def set_question(q):
    # runs when a suggested-question button is clicked
    st.session_state.question_box = q
    st.session_state.auto_run = True


# ---------- streamlit page ----------
st.set_page_config(page_title="AI Data Analyst Assistant", layout="wide")
st.title("AI Data Analyst Assistant")
st.write("Upload a CSV or Excel file and ask questions about it in plain English.")

st.sidebar.header("Settings")
st.sidebar.selectbox("Model", MODEL_OPTIONS, key="model")
st.sidebar.caption("120b is more accurate, 20b is faster.")

api_key = os.environ.get("GROQ_API_KEY")
if not api_key:
    st.error("GROQ_API_KEY is not set. Set it, restart VS Code and run again.")
    st.stop()
client = Groq(api_key=api_key)

for key, default in [("history", []), ("last", None), ("suggestions", None)]:
    if key not in st.session_state:
        st.session_state[key] = default

uploaded = st.file_uploader("Upload a file", type=["csv", "xlsx"])

if uploaded:
    # new file means a fresh start
    if st.session_state.get("file_name") != uploaded.name:
        st.session_state.file_name = uploaded.name
        st.session_state.history = []
        st.session_state.last = None
        st.session_state.suggestions = None

    df = load_data(uploaded)
    st.subheader("Data preview")
    st.dataframe(df.head())
    st.caption(f"{df.shape[0]} rows, {df.shape[1]} columns")

    with st.expander("Data summary (missing values, duplicates, statistics)"):
        show_summary(df)

    if st.session_state.suggestions is None:
        with st.spinner("Thinking of questions to ask..."):
            st.session_state.suggestions = suggest_questions(client, df)

    if st.session_state.suggestions:
        st.write("Suggested questions (click one to run it)")
        for i, q in enumerate(st.session_state.suggestions):
            st.button(q, key=f"suggestion_{i}", on_click=set_question, args=(q,))

    question = st.text_input("Ask a question", key="question_box",
                             placeholder="e.g. Which country has the highest total revenue?")
    analyze = st.button("Analyze")
    auto_run = st.session_state.pop("auto_run", False)

    if (analyze or auto_run) and question:
        with st.spinner("Thinking..."):
            out = answer_question(client, question, df, st.session_state.history)
            note = None
            if not out["error"] and out["result"] is not None:
                note = explain(client, question, out["result"])

        if not out["error"]:
            st.session_state.history.append((question, out["code"]))
        st.session_state.last = {"out": out, "note": note}

    if st.session_state.last:
        show_answer(st.session_state.last)

    if st.session_state.history:
        with st.expander("Previous questions in this conversation"):
            for i, (q, _) in enumerate(st.session_state.history, 1):
                st.write(f"{i}. {q}")
        if st.button("Clear conversation"):
            st.session_state.history = []
            st.session_state.last = None
            st.rerun()