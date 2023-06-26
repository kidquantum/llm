import os
from typing import List, Optional

import streamlit as st
from langchain.vectorstores.base import VectorStore

from llm.qa import model_configs
from llm.qa.embedding import QAEmbeddings, PUBMED_MODEL
from llm.qa.fastchatter import QASession, QueuedEngine, FastchatEngine
from llm.qa.parser import DataFields
from llm.qa.vector_store import DatasetVectorStore
from llm.utils.dataset import load_data

QA_DATASET_PATH = os.environ["QA_DATASET_PATH"]
QA_INDEX_PATH = os.getenv("QA_INDEX_PATH")
QA_CONTEXT_MODEL = os.getenv("QA_CONTEXT_MODEL", PUBMED_MODEL)

st.set_page_config(page_title="pubmed chat", page_icon=":robot_face:", layout="wide")
model_config = model_configs.VICUNA


@st.cache_resource
def get_inference_engine() -> QueuedEngine:
    # Load chat model and inference engine. Shared by all sessions
    model, tokenizer = model_config.load()
    engine = FastchatEngine(model, tokenizer, max_length=model_config.max_length)
    # Wrap with QueuedEngine so each streamlit session has dedicated access during inference
    return QueuedEngine(engine)


@st.cache_resource
def get_vector_store(dataset_path: str, index_path: Optional[str] = None) -> DatasetVectorStore:
    # VectorStore for semantic search. Shared by all sessions
    dataset = load_data(dataset_path)
    embedding = QAEmbeddings(QA_CONTEXT_MODEL)
    return DatasetVectorStore(dataset, embedding, index_path=index_path)


def get_qa_session(engine: QueuedEngine, vector_store: VectorStore) -> QASession:
    # Conversation/contexts for each session
    if "qa_session" not in st.session_state:
        qa_session = QASession.from_model_config(
            model_config, engine, vector_store, debug=True
        )
        st.session_state["qa_session"] = qa_session
        return qa_session
    return st.session_state["qa_session"]


def apply_filter():
    # Filter which contexts are seen by the LLM, and re-ask the last question
    contexts = []
    for doc, to_include in zip(qa_session.results, included):
        if to_include:
            contexts.append(doc.page_content)

    qa_session.clear(keep_results=True)
    qa_session.set_context(contexts)
    qa_session.append_question(user_input)


engine = get_inference_engine()
vector_store = get_vector_store(QA_DATASET_PATH)
qa_session = get_qa_session(engine, vector_store)
output = st.text("")
included: List[bool] = []

clear_convo = st.button("clear conversation", on_click=qa_session.clear)

with st.form(key="input_form", clear_on_submit=True):
    user_input = st.text_area("You:", key="input", height=100)
    question_submit_button = st.form_submit_button(label="Send")
    if question_submit_button and not user_input:
        question_submit_button = False

if question_submit_button and not clear_convo:
    # Write question out to streamlit, then search for contexts
    qa_session.append_question(user_input)
    output.write(qa_session.get_history())
    with st.spinner("Searching..."):
        qa_session.update_context(user_input)

if qa_session.results:
    # Write contexts out to streamlit, with checkboxes to filter what is sent to the LLM
    with st.form(key="checklists"):
        included = []
        for idx, doc in enumerate(qa_session.results):
            include = st.checkbox("include in chat context", key=idx, value=True)
            included.append(include)
            st.write(doc.page_content)
            st.write({k: v for k, v in doc.metadata.items() if k != DataFields.EMBEDDING})
            st.divider()
        checklist_submit_button = st.form_submit_button(label="Filter")

    if checklist_submit_button:
        apply_filter()
else:
    checklist_submit_button = False

if (question_submit_button or checklist_submit_button) and not clear_convo:
    # Stream response from LLM, updating chat window at each step
    message_string = qa_session.get_history()
    for text in qa_session.conversation_stream():
        message = message_string + text
        message = message.replace("\n", "\n\n")
        output.write(message)
