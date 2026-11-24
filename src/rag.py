import chromadb
from dotenv import load_dotenv

from paths import CHROMA_DIR

load_dotenv()


def get_embeddings(api_key: str = None, base_url: str = None, model: str = None):
    """取 RAG 用的 embedding 客户端（LangChain ``OpenAIEmbeddings``）。

    优先级：显式传入参数 > runtime 会话配置 > config_providers 的 RAG 配置。

    ``check_embedding_ctx_length=False`` 是必须的：默认实现会先把文本
    转成 token 数组再发，而百炼 / 智谱这类兼容端点只接受字符串输入，
    不关掉就会报「input 类型错误」。
    """
    from langchain_openai import OpenAIEmbeddings

    import runtime
    from config_providers import get_rag_config

    key = api_key or runtime.get_effective_rag_api_key()
    url = base_url or runtime.get_effective_rag_base_url()

    if not key or not url:
        rag_cfg = get_rag_config()
        key = key or rag_cfg.get("api_key")
        url = url or rag_cfg.get("base_url")

    if not key or key.startswith("sk-placeholder"):
        raise RuntimeError("未配置 RAG API Key。请检查 .env 中的 DASHSCOPE_API_KEY。")

    return OpenAIEmbeddings(
        model=model or _get_embedding_model(url),
        api_key=key,
        base_url=url,
        check_embedding_ctx_length=False,
    )

# ===== Chroma 客户端 =====
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = chroma_client.get_or_create_collection(
    name="agent_documents", metadata={"hnsw:space": "cosine"}
)


# ===== 向量化 =====


def _get_embedding_model(base_url: str = None) -> str:
    """根据 base_url 选 embedding 模型名。"""
    if not base_url:
        return "text-embedding-v4"
    if "bigmodel" in base_url:
        return "embedding-3"
    if "dashscope" in base_url:
        return "text-embedding-v4"
    return "text-embedding-v4"


def embed_texts(texts: list, batch_size: int = 10, api_key: str = None, base_url: str = None) -> list:
    """批量向量化（LangChain ``embed_documents``）。"""
    all_embeddings = []
    import runtime
    effective_url = base_url or runtime.get_effective_rag_base_url()
    embeddings = get_embeddings(api_key, effective_url)
    for i in range(0, len(texts), batch_size):
        all_embeddings.extend(embeddings.embed_documents(texts[i:i + batch_size]))
    return all_embeddings

# ===== 文件文本提取 =====

def extract_text_from_pdf(file_bytes: bytes) -> str:
    """从 PDF 提取纯文本。"""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            text = f"\n[第 {i} 页提取失败：{e}]\n"
        if text.strip():
            pages.append(text)
    return "\n\n".join(pages)


def extract_text_from_docx(file_bytes: bytes) -> str:
    """从 .docx 提取纯文本（含表格）。"""
    import io

    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    parts = []

    # 段落
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    # 表格（把每行拼成 " | " 分隔）
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            line = " | ".join(cells)
            if line.strip(" |"):
                parts.append(line)

    return "\n".join(parts)


def extract_text(filename: str, file_bytes: bytes) -> str:
    """
    根据文件后缀提取纯文本。
    支持：.txt / .md / .pdf / .docx
    抛 ValueError 表示不支持或解析失败。
    """
    name = (filename or "").lower()

    if name.endswith((".txt", ".md")):
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # 兜底：GBK（中文 Windows 常见）
            try:
                return file_bytes.decode("gbk", errors="ignore")
            except Exception:
                raise ValueError("文本文件编码无法识别，请用 UTF-8 保存") from None

    if name.endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
        if not text.strip():
            raise ValueError("PDF 未提取到任何文字（可能是扫描件/图片型 PDF，需要 OCR）")
        return text

    if name.endswith(".docx"):
        text = extract_text_from_docx(file_bytes)
        if not text.strip():
            raise ValueError("DOCX 未提取到任何文字")
        return text

    raise ValueError(f"不支持的文件类型：{filename}")

# ===== 文档切分 =====
def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按字符切分文本，带重叠"""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
        if start >= len(text):
            break
    return [c.strip() for c in chunks if c.strip()]


# ===== 添加文档到向量库 =====
def add_document_to_vectorstore(
    doc_id: int, filename: str, text: str, api_key: str = None, base_url: str = None
) -> int:
    """切分 → 向量化 → 存入 Chroma，返回片段数"""
    chunks = split_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks, api_key=api_key, base_url=base_url)
    ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
    metadatas = [
        {"doc_id": doc_id, "filename": filename, "chunk_index": i} for i in range(len(chunks))
    ]

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=metadatas,
    )
    return len(chunks)


# ===== 检索相关片段 =====
def search_similar(
    query: str, top_k: int = 3, min_score: float = 0.4, api_key: str = None, base_url: str = None
) -> list:
    """
    检索与 query 最相似的 top_k 个片段。
    min_score: 相似度阈值，低于此值过滤掉。
    """
    query_embedding = embed_texts([query], api_key=api_key, base_url=base_url)[0]
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
    )

    out = []
    if results["documents"] and results["documents"][0]:
        for i, doc in enumerate(results["documents"][0]):
            distance = results["distances"][0][i] if "distances" in results else 1
            similarity = 1 - distance

            if similarity < min_score:
                continue

            out.append(
                {
                    "text": doc,
                    "filename": results["metadatas"][0][i].get("filename", ""),
                    "score": round(similarity, 3),
                }
            )
    return out


# ===== 删除文档的所有片段 =====
def delete_document_from_vectorstore(doc_id: int):
    """从 Chroma 删除某个文档的所有片段"""
    collection.delete(where={"doc_id": doc_id})
