import streamlit as st
import os
import io
import time
from datetime import datetime
from pypdf import PdfReader
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="AI PDF Quiz (Streamlit)", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Ubuntu:wght@300;400;500;700&display=swap');

html, body, [class*="css"], [class*="st-"], p, h1, h2, h3, h4, h5, h6, div, span, label, button, input, textarea {
    font-family: 'Ubuntu', sans-serif !important;
}
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def init_db():
    try:
        if not firebase_admin._apps:
            project_id = None
            try:
                if "FIREBASE_PROJECT_ID" in st.secrets:
                    project_id = st.secrets["FIREBASE_PROJECT_ID"]
                    private_key = st.secrets["FIREBASE_PRIVATE_KEY"].replace('\\n', '\n')
                    client_email = st.secrets["FIREBASE_CLIENT_EMAIL"]
            except Exception:
                pass
                
            if not project_id:
                # Fallback to local .env
                from dotenv import load_dotenv
                load_dotenv(".env.local")
                project_id = os.getenv("FIREBASE_PROJECT_ID")
                private_key = os.getenv("FIREBASE_PRIVATE_KEY", "").replace('\\n', '\n')
                client_email = os.getenv("FIREBASE_CLIENT_EMAIL")

            if not project_id: return None

            cred = credentials.Certificate({
                "type": "service_account",
                "project_id": project_id,
                "private_key": private_key,
                "client_email": client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
            })
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        st.error(f"DB Init Error: {e}")
        return None

db = init_db()

def get_gemini_client():
    api_key = None
    try:
        if "GEMINI_API_KEY" in st.secrets:
            api_key = st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
        
    if not api_key:
        api_key = os.getenv("GEMINI_API_KEY")
        
    return genai.Client(api_key=api_key)

class Question(BaseModel):
    id: str = Field(description="Unique ID")
    text: str = Field(description="Question text")
    options: list[str] = Field(description="Options array")
    correctAnswer: str = Field(description="Correct option strictly mapping to item(s) in the options array. Delimit multiple correct choices with comma.")
    explanation: str = Field(description="Markdown explanation of the correct choice.")

class QuestionList(BaseModel):
    questions: list[Question]

# State Variables
if "page" not in st.session_state:
    st.session_state.page = "dashboard"
if "active_source" not in st.session_state:
    st.session_state.active_source = None
if "active_questions" not in st.session_state:
    st.session_state.active_questions = []
if "quiz_idx" not in st.session_state:
    st.session_state.quiz_idx = 0
if "user_answers" not in st.session_state:
    st.session_state.user_answers = {}
if "quiz_start_time" not in st.session_state:
    st.session_state.quiz_start_time = None
if "final_result_id" not in st.session_state:
    st.session_state.final_result_id = None

def navigate(page):
    st.session_state.page = page

@st.dialog("Confirm Deletion")
def confirm_delete_library(source_id):
    st.error("⚠️ Are you sure you want to permanently remove this library and all its associated data?")
    c1, c2 = st.columns(2)
    if c1.button("Yes, Remove", type="primary"):
        with st.spinner("Deleting from database..."):
            if db:
                batch = db.batch()
                
                # Delete Questions
                qs = db.collection("questions").where("sourceId", "==", source_id).stream()
                for q in qs:
                    batch.delete(q.reference)
                    
                # Delete Results
                results = db.collection("results").where("sourceId", "==", source_id).stream()
                for r in results:
                    batch.delete(r.reference)
                    
                # Delete Source Document
                batch.delete(db.collection("sources").document(source_id))
                batch.commit()
                
        time.sleep(1)
        st.rerun()
    if c2.button("No, Cancel"):
        st.rerun()

# --- VIEWS ---

def view_dashboard():
    st.title("📚 Intelligent Document Testing")
    st.markdown("Upload a PDF to instantly generate rigorous AI-authored examinations. (Deployable to Streamlit Cloud)")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("Upload Study Material")
        uploaded_file = st.file_uploader("Select PDF (Max 50 Pages)", type=["pdf"])
        num_questions = st.number_input("Generate How Many Questions?", min_value=1, max_value=30, value=20)
        if st.button("Process Document", type="primary", use_container_width=True):
            if uploaded_file and num_questions > 0:
                with st.spinner(f"Reading {uploaded_file.name} & querying Gemini..."):
                    from pypdf import PdfReader, PdfWriter
                    reader = PdfReader(uploaded_file)
                    writer = PdfWriter()
                    for i in range(min(50, len(reader.pages))):
                        writer.add_page(reader.pages[i])
                    
                    pdf_bytes_io = io.BytesIO()
                    writer.write(pdf_bytes_io)
                    pdf_bytes = pdf_bytes_io.getvalue()
                    
                    try:
                        client = get_gemini_client()
                        prompt = f"Analyze the entire document comprehensively, including any diagrams, pictures, graphs, and formatting. Formulate exactly {num_questions} difficult exam questions testing the core concepts. In your explanations, make sure to completely explain the logic using the formatting and visual information shown in the PDF's pictures/diagrams if relevant."
                        
                        pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf')

                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=[pdf_part, prompt],
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                response_schema=QuestionList,
                            ),
                        )
                        parsed = QuestionList.model_validate_json(response.text)
                        
                        # Save to DB
                        if db:
                            batch = db.batch()
                            source_ref = db.collection("sources").document()
                            batch.set(source_ref, {
                                "fileName": uploaded_file.name,
                                "totalQuestions": len(parsed.questions),
                                "uploadDate": firestore.SERVER_TIMESTAMP
                            })
                            for q in parsed.questions:
                                q_ref = db.collection("questions").document(q.id)
                                q_dict = q.model_dump()
                                q_dict["sourceId"] = source_ref.id
                                batch.set(q_ref, q_dict)
                            batch.commit()
                            st.success(f"Successfully processed {len(parsed.questions)} questions!")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Generation Failed: {e}")

    with col2:
        st.subheader("Your Test Libraries")
        if db:
            docs = list(db.collection("sources").order_by("uploadDate", direction=firestore.Query.DESCENDING).limit(10).stream())
            for d in docs:
                data = d.to_dict()
                with st.container(border=True):
                    st.write(f"**{data.get('fileName')}**")
                    st.caption(f"{data.get('totalQuestions')} questions generated")
                    c_input, c_btn, c_del = st.columns([1.5, 2, 0.5])
                    with c_input:
                        limit = st.number_input("Limit", value=min(20, data.get('totalQuestions', 1)), min_value=1, max_value=data.get('totalQuestions', 1), key=f"lim_{d.id}")
                    with c_btn:
                        st.write("") # spacer
                        if st.button("Start Quiz", key=f"start_{d.id}", use_container_width=True):
                            # Load questions
                            q_docs = list(db.collection("questions").where("sourceId", "==", d.id).limit(limit).stream())
                            q_objs = [{"firebase_id": qd.id, **qd.to_dict()} for qd in q_docs]
                            st.session_state.active_questions = q_objs
                            st.session_state.active_source = d.id
                            st.session_state.quiz_idx = 0
                            st.session_state.user_answers = {}
                            st.session_state.quiz_start_time = time.time()
                            navigate("quiz")
                            st.rerun()
                    with c_del:
                        st.write("") # spacer
                        if st.button("R", key=f"del_{d.id}", help="Remove Library", use_container_width=True):
                            confirm_delete_library(d.id)
                            
    st.divider()
    st.subheader("Score History")
    if db:
        res_docs = list(db.collection("results").order_by("completedAt", direction=firestore.Query.DESCENDING).limit(15).stream())
        if res_docs:
            for r in res_docs:
                r_data = r.to_dict()
                with st.container(border=True):
                    sc, tms = st.columns([3,1])
                    sc.write(f"**Score:** {r_data.get('score')} / {len(r_data.get('answers', {}))} | Time: {r_data.get('totalTime')}s")
                    if tms.button("Review", key=f"rev_{r.id}"):
                        st.session_state.final_result_id = r.id
                        st.session_state.active_source = r_data.get("sourceId")
                        navigate("results")
                        st.rerun()
        else:
            st.info("No scores yet.")

def calculate_score_logic(norm_opt, opt_letter, corr):
    corr_ns = corr.replace(" ", "")
    delimiters_split = corr_ns.replace('and', ',').replace('&', ',').split(',')
    return (norm_opt == corr) or (opt_letter == corr_ns) or (opt_letter in delimiters_split) or (len(corr) > 2 and corr in norm_opt) or (len(norm_opt) > 2 and norm_opt in corr)

def view_quiz():
    st.button("Exit to Dashboard", on_click=lambda: navigate("dashboard"))
    qs = st.session_state.active_questions
    idx = st.session_state.quiz_idx
    
    if idx >= len(qs):
        st.success("Quiz Completed! Submitting...")
        
        # Grading the quiz natively and sending to DB
        answers = st.session_state.user_answers
        time_taken = int(time.time() - st.session_state.quiz_start_time)
        true_score = 0
        
        for q in qs:
            qid = q.get("id") or q.get("firebase_id")
            if qid not in answers: continue
            corr = str(q.get("correctAnswer", "")).lower().strip()
            opts = q.get("options", [])
            u_sels = answers[qid]
            letters = "abcdefghijklmnopqrstuvwxyz"
            
            q_scored = False
            for opt in opts:
                norm_opt = str(opt).lower().strip()
                o_idx = opts.index(opt)
                opt_l = letters[o_idx] if o_idx < len(letters) else "x"
                is_correct = calculate_score_logic(norm_opt, opt_l, corr)
                
                for sel in u_sels:
                    if str(sel).lower().strip() == norm_opt and is_correct:
                        q_scored = True
            if q_scored: true_score += 1
            
        if db:
            doc_ref = db.collection("results").document()
            doc_ref.set({
                "sourceId": st.session_state.active_source,
                "score": true_score,
                "totalTime": time_taken,
                "answers": answers,
                "completedAt": firestore.SERVER_TIMESTAMP
            })
            st.session_state.final_result_id = doc_ref.id
            navigate("results")
            st.rerun()
        return

    q = qs[idx]
    st.progress((idx) / len(qs), text=f"Question {idx+1} of {len(qs)}")
    
    st.markdown(f"### {q['text']}")
    
    # Render Multiselect Checkboxes
    with st.form("quiz_form"):
        selections = []
        for opt in q["options"]:
            if st.checkbox(opt, key=f"chk_{idx}_{opt}"):
                selections.append(opt)
                
        if st.form_submit_button("Submit Answer", type="primary"):
            qid = q.get("id") or q.get("firebase_id")
            st.session_state.user_answers[qid] = selections
            st.session_state.quiz_idx += 1
            st.rerun()

def view_results():
    if st.button("← Back to Dashboard"):
        navigate("dashboard")
        st.rerun()
        
    st.title("Review Your Score")
    if not db or not st.session_state.final_result_id:
        st.error("No result found.")
        return
        
    res_doc = db.collection("results").document(st.session_state.final_result_id).get()
    if not res_doc.exists: return
    r_data = res_doc.to_dict()
    ans = r_data.get("answers", {})
    
    st.success(f"Final Score: {r_data.get('score')} / {len(ans)} • Time: {r_data.get('totalTime')}s")
    
    for q_id, u_sels in ans.items():
        q_doc = db.collection("questions").document(q_id).get()
        if not q_doc.exists: continue
        q = q_doc.to_dict()
        
        with st.container(border=True):
            st.markdown(f"**{q.get('text')}**")
            corr = str(q.get("correctAnswer", "")).lower().strip()
            opts = q.get("options", [])
            letters = "abcdefghijklmnopqrstuvwxyz"
            
            for opt in opts:
                norm_opt = str(opt).lower().strip()
                o_idx = opts.index(opt)
                opt_l = letters[o_idx] if o_idx < len(letters) else "x"
                is_correct = calculate_score_logic(norm_opt, opt_l, corr)
                
                is_user = False
                for sel in u_sels:
                    if str(sel).lower().strip() == norm_opt:
                        is_user = True
                
                if is_correct and is_user:
                    st.success(f"✅ (Correct & Selected) {opt}")
                elif is_correct and not is_user:
                    st.info(f"☑️ (Correct Answer Missed) {opt}")
                elif not is_correct and is_user:
                    st.error(f"❌ (Wrongly Selected) {opt}")
                else:
                    st.write(f"⬜ {opt}")
                    
            if q.get("explanation"):
                st.markdown("---")
                st.markdown(f"**Explanation:**\n\n{q.get('explanation')}")

# Router
if st.session_state.page == "dashboard": view_dashboard()
elif st.session_state.page == "quiz": view_quiz()
elif st.session_state.page == "results": view_results()
