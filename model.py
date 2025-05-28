# Adaptive Med-RAG: A Textual Retrieval-Augmented Generation Pipeline
# with CoT/GoT/SoRA Implementation

import os
import re
import time
import json
import hashlib
import numpy as np
import pandas as pd
from typing import List, Dict, Tuple, Any, Optional, Union
from dataclasses import dataclass, field

# External dependencies
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import (
    AutoTokenizer, 
    AutoModelForCausalLM, 
    BitsAndBytesConfig,
    TrainingArguments,
)
from peft import LoraConfig, get_peft_model
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains import RetrievalQA
from sklearn.metrics.pairwise import cosine_similarity
from tqdm.auto import tqdm
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS, Chroma
from langchain_community.document_loaders import PyPDFLoader, TextLoader, DirectoryLoader


# Load model directly
from transformers import AutoTokenizer, AutoModelForCausalLM


# Configurables
CONFIG = {
    "base_model": "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext",
    "medical_lm": "epfl-llm/meditron-7b",  # Fine-tuned medical LLM
    "embedding_model": "pritamdeka/S-PubMedBert-MS-MARCO",
    "device": "cuda" if torch.cuda.is_available() else "cpu",
    "data_dir": "./medical_data",
    "index_path": "./vector_indexes",
    "cache_dir": "./retrieval_cache",
    "max_length": 512,
    "chunk_size": 384,
    "chunk_overlap": 128,
    "top_k_default": 5,
    "temperature": 0.1,
}

# from transformers import BitsAndBytesConfig

# quant_config = BitsAndBytesConfig(load_in_8bit=True)
# model = AutoModelForCausalLM.from_pretrained(CONFIG["medical_lm"], quantization_config=quant_config)
# tokenizer = AutoTokenizer.from_pretrained(CONFIG["medical_lm"])

# Setup logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("adaptive-med-rag")

# ------------------------------ DEBUGGING: CHECK FILES ------------------------------
print("Checking medical_data directory...")
print(f"Files present: {os.listdir('./medical_data/')}")

# Load and print the first few lines of the file
file_path = "./medical_data/sample_medical_info.txt"
if os.path.exists(file_path):
    with open(file_path, "r") as f:
        text = f.read()
    print(f"Loaded document content (first 500 chars):\n{text[:500]}")
else:
    print("Error: sample_medical_info.txt is missing!")


# ------------------------------ CLASS DEFINITION ------------------------------

class MedicalDataProcessor:
    """Process and load medical documents for RAG system"""

    def __init__(self, data_dir: str, chunk_size: int = 384, chunk_overlap: int = 100):
        self.data_dir = data_dir
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", " ", ""]
        )

    def load_documents(self) -> List:
        """Load documents from multiple sources"""
        logger.info(f"Loading documents from {self.data_dir}")

        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
            logger.warning(f"Data directory {self.data_dir} was created but empty")
            return []

        try:
            loader = DirectoryLoader(
                self.data_dir, glob="*.txt", loader_cls=TextLoader
            )
            documents = loader.load()
            logger.info(f"✅ Successfully loaded {len(documents)} documents.")
            return documents
        except Exception as e:
            logger.error(f"❌ Error loading documents: {e}")
            return []

    def split_documents(self, documents: List) -> List:
        """Split documents into chunks for embedding"""
        logger.info(f"Splitting {len(documents)} documents into chunks")

        if not documents:
            logger.error("❌ No documents provided to split.")
            return []

        # Try chunking
        chunks = self.text_splitter.split_documents(documents)

        if not chunks:
            print("❌ No chunks were created! Check text splitting logic.")
        else:
            print(f"✅ Successfully created {len(chunks)} chunks.")
            for i, chunk in enumerate(chunks[:3]):  # Print first 3 chunks
                print(f"🔹 Chunk {i+1}:\n{chunk.page_content}\n")

        return chunks

    def process(self) -> list:
        """Process all documents: load and split into chunks"""
        documents = self.load_documents()

        # 🚀 Debug: Print document count
        print(f"✅ Debug: Loaded {len(documents)} documents.")

        if documents:
            print(f"🔹 First document preview: {documents[0].page_content[:200]}")

        chunks = self.split_documents(documents)

        # 🚀 Debug: Print chunk count
        print(f"✅ Debug: Created {len(chunks)} chunks.")

        if chunks:
            print(f"🔹 First chunk preview: {chunks[0].page_content[:200]}")

        return chunks


# ------------------------------ VECTOR STORE & EMBEDDINGS ------------------------------

class MedicalVectorStore:
    """Manage vector embeddings and retrieval for medical documents"""

    def __init__(self, embedding_model_name: str, index_path: str):
        self.embedding_model_name = embedding_model_name
        self.index_path = index_path
        self.embeddings = self._init_embeddings()

    def _init_embeddings(self):
        """Initialize the embedding model"""
        logger.info(f"Initializing embeddings with model: {self.embedding_model_name}")
        model_kwargs = {'device': CONFIG.get('device', 'cpu')}  # Default to CPU
        encode_kwargs = {'normalize_embeddings': True}

        return HuggingFaceEmbeddings(
            model_name=self.embedding_model_name,
            model_kwargs=model_kwargs,
            encode_kwargs=encode_kwargs
        )

    def create_or_load_index(self, chunks=None):
        """Create a new vector index or load an existing one"""
        faiss_index_file = os.path.join(self.index_path, "index.faiss")
        faiss_config_file = os.path.join(self.index_path, "config.pkl")

        # Debug: Print the number of chunks received
        print(f"🚀 Debug: Received {len(chunks) if chunks else 0} chunks for indexing.")

        if os.path.exists(faiss_index_file) and os.path.exists(faiss_config_file):
            logger.info(f"Loading existing vector store from {self.index_path}")
            try:
                return FAISS.load_local(self.index_path, self.embeddings)
            except Exception as e:
                logger.error(f"Failed to load FAISS index: {e}")
                raise RuntimeError("Error loading FAISS index, consider rebuilding it.")

        if not chunks:
            raise ValueError("❌ No chunks provided to create new vector index. Check document processing.")

        logger.info(f"Creating new vector index with {len(chunks)} chunks")
        vector_store = FAISS.from_documents(chunks, self.embeddings)

        # Save the vector store
        os.makedirs(self.index_path, exist_ok=True)
        vector_store.save_local(self.index_path)
        logger.info(f"✅ Saved vector store to {self.index_path}")

        return vector_store

# ------------------------------ ADAPTIVE RAG COMPONENTS ------------------------------

@dataclass
class RetrievalResult:
    """Store retrieval results with metadata for analysis"""
    query: str
    documents: List[Any]
    embedding: Optional[List[float]] = None
    similarity_scores: Optional[List[float]] = None
    retrieval_time: float = 0.0
    source: str = "default"  # Source of this retrieval (default, cot-step, contrastive, etc.)
    
    def get_context_text(self) -> str:
        """Combine all document texts into a single context"""
        return "\n\n".join([doc.page_content for doc in self.documents])
    
    def get_sources(self) -> List[str]:
        """Get list of document sources"""
        sources = []
        for doc in self.documents:
            if hasattr(doc, 'metadata') and 'source' in doc.metadata:
                sources.append(doc.metadata['source'])
        return sources

class LatentMemoryAdapter:
    """Cache retrieval results to avoid redundant retrievals"""
    
    def __init__(self, cache_dir: str, ttl_hours: float = 24.0):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)
        
    def _get_cache_key(self, query: str) -> str:
        """Generate a deterministic key from query"""
        return hashlib.md5(query.encode('utf-8')).hexdigest()
    
    def _get_cache_path(self, key: str) -> str:
        """Get file path for cache key"""
        return os.path.join(self.cache_dir, f"{key}.json")
    
    def get(self, query: str) -> Optional[RetrievalResult]:
        """Retrieve cached results if available and not expired"""
        key = self._get_cache_key(query)
        cache_path = self._get_cache_path(key)
        
        if not os.path.exists(cache_path):
            return None
            
        # Check if cache is expired
        cache_time = os.path.getmtime(cache_path)
        if time.time() - cache_time > self.ttl_seconds:
            os.remove(cache_path)
            return None
            
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
                
            # Reconstruct RetrievalResult
            from langchain.schema import Document
            documents = [
                Document(page_content=doc['content'], metadata=doc['metadata'])
                for doc in data['documents']
            ]
            
            return RetrievalResult(
                query=data['query'],
                documents=documents,
                embedding=data.get('embedding'),
                similarity_scores=data.get('similarity_scores'),
                retrieval_time=data.get('retrieval_time', 0.0),
                source=data.get('source', 'cache')
            )
        except Exception as e:
            logger.warning(f"Error loading cache: {e}")
            return None
    
    def save(self, result: RetrievalResult):
        """Cache retrieval result"""
        key = self._get_cache_key(result.query)
        cache_path = self._get_cache_path(key)
        
        # Convert to serializable format
        serialized_docs = []
        for doc in result.documents:
            serialized_docs.append({
                'content': doc.page_content,
                'metadata': doc.metadata if hasattr(doc, 'metadata') else {}
            })
            
        data = {
            'query': result.query,
            'documents': serialized_docs,
            'embedding': result.embedding,
            'similarity_scores': result.similarity_scores,
            'retrieval_time': result.retrieval_time,
            'source': result.source,
            'cached_at': time.time()
        }
        
        with open(cache_path, 'w') as f:
            json.dump(data, f)

class AdaptiveRetriever:
    """Base retriever with adaptive capabilities"""
    
    def __init__(self, vector_store, memory_adapter: LatentMemoryAdapter, 
                 default_k: int = 5, min_score_threshold: float = 0.6):
        self.vector_store = vector_store
        self.memory = memory_adapter
        self.default_k = default_k
        self.min_score_threshold = min_score_threshold
        
    def retrieve(self, query: str, k: Optional[int] = None, 
                 source: str = "default", use_cache: bool = True) -> RetrievalResult:
        """Retrieve relevant documents for a query"""
        k = k or self.default_k
        
        # Check cache first if enabled
        if use_cache:
            cached = self.memory.get(query)
            if cached:
                logger.info(f"Retrieved from cache: {query[:50]}...")
                return cached
        
        # Perform retrieval
        start_time = time.time()
        docs_with_scores = self.vector_store.similarity_search_with_score(query, k=k)
        
        # Process results
        documents = []
        scores = []
        for doc, score in docs_with_scores:
            if score >= self.min_score_threshold:
                documents.append(doc)
                scores.append(float(score))
        
        # Get query embedding for further analysis
        query_embedding = self.vector_store.embeddings.embed_query(query)
        
        result = RetrievalResult(
            query=query,
            documents=documents,
            embedding=query_embedding,
            similarity_scores=scores,
            retrieval_time=time.time() - start_time,
            source=source
        )
        
        # Cache result
        if use_cache:
            self.memory.save(result)
            
        return result
    
    def analyze_retrieval_quality(self, result: RetrievalResult) -> Dict:
        """Analyze retrieval quality metrics"""
        if not result.documents:
            return {"quality": "insufficient", "avg_score": 0, "score_variance": 0}
            
        avg_score = sum(result.similarity_scores) / len(result.similarity_scores)
        score_variance = np.var(result.similarity_scores) if len(result.similarity_scores) > 1 else 0
        
        # Determine quality level
        quality = "high" if avg_score > 0.8 else "medium" if avg_score > 0.65 else "low"
        
        return {
            "quality": quality,
            "avg_score": avg_score,
            "score_variance": score_variance,
            "docs_retrieved": len(result.documents),
            "retrieval_time_ms": result.retrieval_time * 1000
        }

class SelfOptimizingAdaptiveRetriever(AdaptiveRetriever):
    """SOAR: Self-Optimizing Adaptive Retrieval implementation"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_k = kwargs.get('max_k', 20)
        self.min_k = kwargs.get('min_k', 3)
        self.step_size = kwargs.get('step_size', 2)
        
    def adaptive_retrieve(self, query: str, initial_k: Optional[int] = None) -> RetrievalResult:
        """Dynamically adjust retrieval depth based on quality assessment"""
        k = initial_k or self.default_k
        
        # Initial retrieval
        result = self.retrieve(query, k=k, source="soar-initial")
        quality = self.analyze_retrieval_quality(result)
        
        # Adaptive behavior based on quality
        if quality["quality"] == "low" and k < self.max_k:
            logger.info(f"Low quality retrieval ({quality['avg_score']:.2f}), increasing depth")
            # Try retrieving more documents
            new_k = min(k + self.step_size, self.max_k)
            enhanced_result = self.retrieve(query, k=new_k, source="soar-enhanced")
            enhanced_quality = self.analyze_retrieval_quality(enhanced_result)
            
            # Take the better result
            if enhanced_quality["avg_score"] > quality["avg_score"]:
                return enhanced_result
        
        return result

class CoTRetriever:
    """Chain-of-Thought Retrieval: Break query into steps"""
    
    def __init__(self, base_retriever: AdaptiveRetriever, language_model):
        self.base_retriever = base_retriever
        self.lm = language_model
        
    def _generate_retrieval_steps(self, query: str) -> List[str]:
        """Generate intermediate retrieval steps using the LM"""
        prompt = f"""
        Break down the following medical query into smaller, logical sub-questions 
        that would help progressively answer the full question. For each step, 
        provide just the specific sub-question without explanations.
        
        Original query: {query}
        
        Format your response as:
        1. [first sub-question]
        2. [second sub-question]
        ...and so on
        """
        
        response = self.lm.generate(prompt, max_length=512)
        
        # Parse the steps from the response
        steps = []
        for line in response.strip().split('\n'):
            match = re.match(r'^\d+\.\s+(.+)$', line.strip())
            if match:
                steps.append(match.group(1))
        
        return steps
    
    def multi_hop_retrieve(self, query: str) -> List[RetrievalResult]:
        """Perform multi-hop retrieval through logical steps"""
        steps = self._generate_retrieval_steps(query)
        logger.info(f"CoT retrieval generated {len(steps)} steps for query")
        
        results = []
        for i, step in enumerate(steps):
            step_result = self.base_retriever.retrieve(
                step, source=f"cot-step-{i+1}"
            )
            results.append(step_result)
            
        # Add original query as final step
        final_result = self.base_retriever.retrieve(
            query, source="cot-final"
        )
        results.append(final_result)
        
        return results
    
    def get_combined_context(self, results: List[RetrievalResult]) -> str:
        """Combine contexts from multi-hop retrieval with step markers"""
        combined = []
        
        for i, result in enumerate(results):
            source_marker = f"[Step {i+1}]" if i < len(results) - 1 else "[Final]"
            step_context = f"{source_marker}\n{result.get_context_text()}"
            combined.append(step_context)
            
        return "\n\n".join(combined)

class ContrastiveRetriever:
    """Retrieve opposing or diverse perspectives on medical topics"""
    
    def __init__(self, base_retriever: AdaptiveRetriever, language_model):
        self.base_retriever = base_retriever
        self.lm = language_model
        
    def _generate_contrasting_queries(self, query: str) -> List[str]:
        """Generate contrasting or alternative perspectives for a query"""
        prompt = f"""
        For the following medical question or topic, generate 2-3 alternative 
        perspectives or opposing viewpoints that should be considered to provide 
        a balanced answer. Focus on legitimate medical perspectives, not fringe views.
        
        Original query: {query}
        
        Format your response as:
        1. [first alternative perspective/query]
        2. [second alternative perspective/query]
        ...and so on
        """
        
        response = self.lm.generate(prompt, max_length=512)
        
        # Parse the alternative perspectives
        alternatives = []
        for line in response.strip().split('\n'):
            match = re.match(r'^\d+\.\s+(.+)$', line.strip())
            if match:
                alternatives.append(match.group(1))
        
        return alternatives
    
    def retrieve_contrasting_perspectives(self, query: str) -> List[RetrievalResult]:
        """Retrieve documents representing diverse viewpoints"""
        # Get the main retrieval
        main_result = self.base_retriever.retrieve(
            query, source="contrastive-main"
        )
        
        # Get alternative perspectives
        alternative_queries = self._generate_contrasting_queries(query)
        alternative_results = []
        
        for i, alt_query in enumerate(alternative_queries):
            alt_result = self.base_retriever.retrieve(
                alt_query, source=f"contrastive-alt-{i+1}"
            )
            alternative_results.append(alt_result)
            
        # Combine main result with alternatives
        all_results = [main_result] + alternative_results
        return all_results
    
    def get_balanced_context(self, results: List[RetrievalResult]) -> str:
        """Combine contrasting perspectives with clear section markers"""
        combined = []
        
        # Main perspective first
        combined.append(f"[Main Perspective]\n{results[0].get_context_text()}")
        
        # Alternative perspectives
        for i, result in enumerate(results[1:], 1):
            combined.append(f"[Alternative Perspective {i}]\n{result.get_context_text()}")
            
        return "\n\n".join(combined)

class SelfConsistencyRAG:
    """Generate multiple responses and select the most consistent one"""
    
    def __init__(self, base_retriever: AdaptiveRetriever, language_model, 
                 num_samples: int = 3, similarity_threshold: float = 0.8):
        self.base_retriever = base_retriever
        self.lm = language_model
        self.num_samples = num_samples
        self.similarity_threshold = similarity_threshold
        
    def _calculate_similarity(self, response1: str, response2: str) -> float:
        """Calculate similarity between two responses"""
        # Get embeddings
        emb1 = self.base_retriever.vector_store.embeddings.embed_query(response1)
        emb2 = self.base_retriever.vector_store.embeddings.embed_query(response2)
        
        # Calculate cosine similarity
        similarity = cosine_similarity([emb1], [emb2])[0][0]
        return float(similarity)
    
    def _generate_response(self, query: str, context: str) -> str:
        """Generate a single response using the language model"""
        prompt = f"""
        Please provide a precise, factual answer to the medical question based only on 
        the provided context. If the context doesn't contain sufficient information, 
        state this clearly rather than speculating.
        
        Context:
        {context}
        
        Question: {query}
        
        Answer:
        """
        
        return self.lm.generate(prompt, max_length=512)
    
    def generate_consistent_response(self, query: str) -> Tuple[str, float]:
        """Generate multiple responses and select the most consistent one"""
        # Get retrieval result
        result = self.base_retriever.retrieve(query)
        context = result.get_context_text()
        
        # Generate multiple responses
        responses = []
        for _ in range(self.num_samples):
            response = self._generate_response(query, context)
            responses.append(response)
            
        # Calculate pairwise similarities
        similarities = []
        for i in range(len(responses)):
            for j in range(i+1, len(responses)):
                sim = self._calculate_similarity(responses[i], responses[j])
                similarities.append((i, j, sim))
                
        # Find most consistent response (highest average similarity to others)
        avg_similarities = [0] * len(responses)
        for i, j, sim in similarities:
            avg_similarities[i] += sim
            avg_similarities[j] += sim
            
        # Normalize by the number of comparisons per response
        for i in range(len(avg_similarities)):
            avg_similarities[i] /= (len(responses) - 1)
            
        # Get most consistent response
        best_idx = np.argmax(avg_similarities)
        best_response = responses[best_idx]
        consistency_score = avg_similarities[best_idx]
        
        return best_response, consistency_score

class PersonalizedRAGAdapter:
    """Learn and adapt to user preferences over time"""
    
    def __init__(self, base_retriever: AdaptiveRetriever, user_id: str, 
                 preferences_path: str = "./user_preferences"):
        self.base_retriever = base_retriever
        self.user_id = user_id
        self.preferences_path = preferences_path
        self.preferences = self._load_preferences()
        
    def _get_user_file_path(self) -> str:
        """Get path to user preferences file"""
        os.makedirs(self.preferences_path, exist_ok=True)
        return os.path.join(self.preferences_path, f"{self.user_id}.json")
    
    def _load_preferences(self) -> Dict:
        """Load user preferences from file or create defaults"""
        file_path = self._get_user_file_path()
        
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        
        # Default preferences
        return {
            "preferred_sources": [],
            "source_weights": {},
            "topic_interests": {},
            "retrieval_depth": self.base_retriever.default_k,
            "interaction_history": []
        }
    
    def _save_preferences(self):
        """Save user preferences to file"""
        file_path = self._get_user_file_path()
        with open(file_path, 'w') as f:
            json.dump(self.preferences, f)
    
    def record_interaction(self, query: str, sources_used: List[str], 
                           feedback_score: Optional[float] = None):
        """Record user interaction for preference learning"""
        # Extract potential topics using simple keyword matching
        # In a real system, this would use more sophisticated topic extraction
        topics = self._extract_topics(query)
        
        # Record interaction
        interaction = {
            "timestamp": time.time(),
            "query": query,
            "sources_used": sources_used,
            "topics": topics,
            "feedback_score": feedback_score
        }
        
        self.preferences["interaction_history"].append(interaction)
        
        # Update topic interests
        for topic in topics:
            if topic not in self.preferences["topic_interests"]:
                self.preferences["topic_interests"][topic] = 1
            else:
                self.preferences["topic_interests"][topic] += 1
                
        # Update source preferences if feedback provided
        if feedback_score is not None and feedback_score > 0:
            for source in sources_used:
                if source not in self.preferences["source_weights"]:
                    self.preferences["source_weights"][source] = feedback_score
                else:
                    # Weighted average favoring recent feedback
                    current = self.preferences["source_weights"][source]
                    self.preferences["source_weights"][source] = 0.7 * feedback_score + 0.3 * current
        
        # Save updated preferences
        self._save_preferences()
    
    def _extract_topics(self, query: str) -> List[str]:
        """Extract medical topics from query"""
        # Simple keyword extraction - would use NER or topic modeling in production
        common_medical_topics = [
            "diabetes", "cancer", "heart disease", "covid", "vaccine",
            "medication", "surgery", "treatment", "diagnosis", "symptoms",
            "pediatric", "geriatric", "emergency", "chronic", "acute"
        ]
        
        found_topics = []
        query_lower = query.lower()
        
        for topic in common_medical_topics:
            if topic in query_lower:
                found_topics.append(topic)
                
        return found_topics
    
    def personalized_retrieve(self, query: str) -> RetrievalResult:
        """Retrieve with personalized adjustments"""
        # Adjust retrieval depth based on user preference
        k = self.preferences.get("retrieval_depth", self.base_retriever.default_k)
        
        # Perform base retrieval
        result = self.base_retriever.retrieve(query, k=k, source="personalized")
        
        # Record this interaction (without feedback initially)
        self.record_interaction(query, result.get_sources())
        
        return result
    
    def record_feedback(self, query: str, sources_used: List[str], feedback_score: float):
        """Record explicit user feedback"""
        self.record_interaction(query, sources_used, feedback_score)

# ------------------------------ LLM INTERFACE ------------------------------

class MedicalLanguageModel:
    """Interface to medical LLM"""

    def __init__(self, model_name: str, device: str = "auto", 
                 temperature: float = 0.7, max_length: int = 512):
        self.model_name = model_name
        self.device = device if torch.cuda.is_available() else "cpu"  # ✅ Auto-detect device
        self.temperature = temperature
        self.max_length = max_length
        self.tokenizer, self.model = self._load_model()

    def _load_model(self):
        """Load the language model"""
        logger.info(f"🚀 Loading medical LLM: {self.model_name} on {self.device}")

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            device_map=self.device,  # ✅ Correctly map device
            trust_remote_code=True
        )

        logger.info(f"✅ Model loaded successfully on {self.device}")
        return tokenizer, model

    def generate(self, prompt: str, max_length: Optional[int] = None) -> str:
        """Generate text with the medical LLM"""
        max_tokens = max_length or self.max_length
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)

        # Generate with the model
        with torch.no_grad():
            outputs = self.model.generate(
                inputs.input_ids,
                max_length=max_tokens,
                temperature=self.temperature,
                do_sample=True,
                top_p=0.95,
                pad_token_id=self.tokenizer.eos_token_id
            )

        # Decode and clean the output
        response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)

        # ✅ Improved prompt cleaning
        if prompt.lower() in response.lower():
            response = response[len(prompt):].strip()

        return response

# ------------------------------ ADAPTIVE MED-RAG MAIN SYSTEM ------------------------------

class AdaptiveMedRAG:
    """Main system combining all RAG innovations"""
    
    def __init__(self, config=None):
        if config is None:
            raise ValueError("❌ Config cannot be None. Ensure you provide a valid configuration.")

        self.config = config
        
        # Log config keys for debugging
        logger.info(f"Loaded Config Keys: {list(self.config.keys())}")
        
        # Initialize components
        self._init_components()
        

    def _init_components(self):
        """Initialize all system components"""
        logger.info("🔧 Initializing Adaptive Med-RAG components...")
        
        # 1. Data Processor
        self.data_processor = MedicalDataProcessor(
            self.config.get("data_dir", "./data"),
            chunk_size=self.config.get("chunk_size", 512),
            chunk_overlap=self.config.get("chunk_overlap", 50)
        )
        
        # 2. Process Documents
        logger.info("📂 Processing documents for chunking...")
        chunks = self.data_processor.process()
        
        if not chunks:
            logger.error("❌ No valid documents found in data_dir. Please check input files.")
            raise ValueError("No valid documents found for processing.")

        # 3. Vector Store
        self.vector_store = MedicalVectorStore(
            self.config.get("embedding_model", "default-embedding"),
            self.config.get("index_path", "./index")
        )
        
        # 4. Create or Load Index
        logger.info(f"📌 Creating/loading index with {len(chunks)} chunks...")
        try:
            self.vector_store.create_or_load_index(chunks)
        except Exception as e:
            logger.error(f"❌ Failed to create/load index: {e}")
            raise
        
        # 5. Latent Memory Adapter
        self.memory = LatentMemoryAdapter(self.config.get("cache_dir", "./cache"))
        
        # 6. Base Retriever
        self.base_retriever = AdaptiveRetriever(
            self.vector_store,
            self.memory,
            default_k=self.config.get("top_k_default", 5)
        )
        
        # 7. Language Model (Safe Initialization)
        try:
            self.lm = MedicalLanguageModel(
                self.config.get("medical_lm", "default-medical-lm"),
                device=self.config.get("device", "cpu"),
                temperature=self.config.get("temperature", 0.7)
            )
        except Exception as e:
            logger.error(f"❌ Failed to load MedicalLanguageModel: {e}")
            raise
        
        # 8. Advanced Retrievers
        self.soar = SelfOptimizingAdaptiveRetriever(
            self.vector_store,
            self.memory,
            default_k=self.config.get("top_k_default", 5)
        )
        
        self.cot_retriever = CoTRetriever(self.base_retriever, self.lm)
        self.contrastive = ContrastiveRetriever(self.base_retriever, self.lm)
        self.sc_rag = SelfConsistencyRAG(self.base_retriever, self.lm)
        
        logger.info("✅ All components initialized successfully")

    
    def index_documents(self):
        """Process and index documents for the RAG system"""
        logger.info("📂 Processing and indexing medical documents...")
        
        # Process documents
        chunks = self.data_processor.process()
        
        if not chunks:
            logger.warning("⚠️ No documents found to index. Skipping indexing.")
            return False
            
        # Create vector index
        try:
            self.vector_store.create_or_load_index(chunks)
            logger.info(f"✅ Successfully indexed {len(chunks)} document chunks")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to index documents: {e}")
            return False
    
    def _choose_retrieval_strategy(self, query: str) -> str:
        """Determine best retrieval strategy based on query analysis"""
        query_lower = query.lower()

        # Strategy conditions
        if any(term in query_lower for term in ["versus", "vs", "compared to", "difference between"]):
            return "contrastive"
            
        if any(term in query_lower for term in ["why", "how", "explain", "mechanism", "pathway"]):
            return "cot"
            
        if "?" in query and len(query.split()) > 15:
            return "soar"
            
        return "standard"
        
    def answer_medical_query(self, query: str, user_id: Optional[str] = None, 
                             strategy: Optional[str] = None, explain: bool = False) -> Dict:
        """Main entry point for answering medical queries"""
        logger.info(f"🔎 Processing medical query: {query[:50]}...")
        
        # 1. Determine retrieval strategy
        if not strategy:
            strategy = self._choose_retrieval_strategy(query)
            logger.info(f"📌 Selected strategy: {strategy}")
        
        # 2. Execute appropriate retrieval strategy
        retrieval_result, context, retrieval_info = None, "", {}
        
        try:
            if strategy == "standard":
                retrieval_result = self.base_retriever.retrieve(query)
                context = retrieval_result.get_context_text()
                retrieval_info = {"strategy": "standard", "docs_retrieved": len(retrieval_result.documents)}
                
            elif strategy == "soar":
                retrieval_result = self.soar.adaptive_retrieve(query)
                context = retrieval_result.get_context_text()
                retrieval_info = {"strategy": "soar", "docs_retrieved": len(retrieval_result.documents)}
                
            elif strategy == "cot":
                retrieval_results = self.cot_retriever.multi_hop_retrieve(query)
                context = self.cot_retriever.get_combined_context(retrieval_results)
                retrieval_info = {"strategy": "cot", "steps": len(retrieval_results)}
                
            elif strategy == "contrastive":
                retrieval_results = self.contrastive.retrieve_contrasting_perspectives(query)
                context = self.contrastive.get_balanced_context(retrieval_results)
                retrieval_info = {"strategy": "contrastive", "perspectives": len(retrieval_results)}
                
            elif strategy == "consistency":
                answer, consistency_score = self.sc_rag.generate_consistent_response(query)
                return {"query": query, "answer": answer, "retrieval": {"strategy": "consistency"}, "sources": ["Self-consistency RAG"]}
            
            else:
                raise ValueError(f"Unknown retrieval strategy: {strategy}")
        except Exception as e:
            logger.error(f"❌ Retrieval failed: {e}")
            return {"query": query, "error": "Retrieval failed"}
        
        # 3. Generate answer
        prompt = f"""
        Please answer the following medical question based on the retrieved context.
        
        Context:
        {context}
        
        Question: {query}
        
        Answer:
        """
        
        try:
            answer = self.lm.generate(prompt)
        except Exception as e:
            logger.error(f"❌ Language model generation failed: {e}")
            return {"query": query, "error": "Language model generation failed"}
        
        # 4. Extract sources
        sources = retrieval_result.get_sources() if retrieval_result else []
        
        # 5. Return complete response
        return {"query": query, "answer": answer, "retrieval": retrieval_info, "sources": sources}
# ------------------------------ EVALUATION & BENCHMARKING ------------------------------

class MedicalRAGEvaluator:
    """Evaluate RAG system performance on medical questions"""
    
    def __init__(self, med_rag: AdaptiveMedRAG):
        self.med_rag = med_rag
        
    def evaluate_on_dataset(self, dataset_path: str) -> Dict:
        """Evaluate on a dataset of medical questions"""
        logger.info(f"Evaluating on dataset: {dataset_path}")
        
        # Load dataset
        df = pd.read_csv(dataset_path)
        
        results = {
            "total_questions": len(df),
            "strategies": {},
            "accuracy": 0.0,
            "detailed": []
        }
        
        for index, row in tqdm(df.iterrows(), total=len(df)):
            query = row["question"]
            expected = row["answer"]
            
            # Get adaptive strategy
            strategy = self.med_rag._choose_retrieval_strategy(query)
            
            # Track strategy usage
            if strategy not in results["strategies"]:
                results["strategies"][strategy] = 0
            results["strategies"][strategy] += 1
            
            # Get answer
            response = self.med_rag.answer_medical_query(query, strategy=strategy)
            answer = response["answer"]
            
            # Calculate simple string match score
            # In a real system, would use more sophisticated evaluation metrics
            accuracy = self._calculate_answer_accuracy(answer, expected)
            
            # Record detailed result
            results["detailed"].append({
                "query": query,
                "strategy": strategy,
                "expected": expected,
                "actual": answer,
                "accuracy": accuracy
            })
        
        # Calculate overall accuracy
        if results["detailed"]:
            results["accuracy"] = sum(item["accuracy"] for item in results["detailed"]) / len(results["detailed"])
        
        logger.info(f"Evaluation complete. Overall accuracy: {results['accuracy']:.4f}")
        return results
    
    def _calculate_answer_accuracy(self, actual: str, expected: str) -> float:
        """Simple accuracy calculation between actual and expected answer"""
        # In production would use more sophisticated metrics like ROUGE, BLEU, BERTScore
        
        # Basic string overlap calculation
        actual_words = set(actual.lower().split())
        expected_words = set(expected.lower().split())
        
        if not expected_words:
            return 0.0
            
        overlap = len(actual_words.intersection(expected_words))
        return overlap / len(expected_words)
    
    def compare_strategies(self, test_queries: List[str]) -> Dict:
        """Compare different retrieval strategies on the same queries"""
        strategies = ["standard", "soar", "cot", "contrastive", "consistency"]
        
        results = {
            "queries": len(test_queries),
            "strategy_comparison": {}
        }
        
        for query in tqdm(test_queries):
            query_results = {}
            
            for strategy in strategies:
                # Get answer with this strategy
                start_time = time.time()
                response = self.med_rag.answer_medical_query(query, strategy=strategy)
                elapsed = time.time() - start_time
                
                query_results[strategy] = {
                    "answer": response["answer"],
                    "retrieval_info": response["retrieval"],
                    "time_seconds": elapsed
                }
            
            # Calculate cross-strategy similarity
            similarities = {}
            for i, s1 in enumerate(strategies):
                for j, s2 in enumerate(strategies[i+1:], i+1):
                    key = f"{s1}_vs_{s2}"
                    
                    # Calculate similarity between answers
                    a1 = query_results[s1]["answer"]
                    a2 = query_results[s2]["answer"]
                    
                    # Get embeddings
                    emb1 = self.med_rag.vector_store.embeddings.embed_query(a1)
                    emb2 = self.med_rag.vector_store.embeddings.embed_query(a2)
                    
                    # Calculate cosine similarity
                    sim = cosine_similarity([emb1], [emb2])[0][0]
                    similarities[key] = float(sim)
            
            # Record results for this query
            results["strategy_comparison"][query] = {
                "strategy_results": query_results,
                "cross_similarities": similarities
            }
        
        return results


# ------------------------------ CLI & APPLICATION ------------------------------

def setup_argparse():
    """Set up command line argument parsing"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Adaptive Med-RAG: Medical Retrieval Augmented Generation")
    
    parser.add_argument("--index", action="store_true", help="Index medical documents")
    parser.add_argument("--query", type=str, help="Answer a medical query")
    parser.add_argument("--strategy", type=str, choices=["standard", "soar", "cot", "contrastive", "consistency"],
                      help="Force a specific retrieval strategy")
    parser.add_argument("--user", type=str, help="User ID for personalized retrieval")
    parser.add_argument("--evaluate", type=str, help="Path to evaluation dataset")
    parser.add_argument("--compare", type=str, help="Path to test queries for strategy comparison")
    parser.add_argument("--explain", action="store_true", help="Include explanations in the answer")
    parser.add_argument("--config", type=str, help="Path to config file")
    
    return parser

def load_config(config_path=None):
    """Load configuration from file or use defaults"""
    if config_path and os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return json.load(f)
    return CONFIG

def main():
    """Main entry point for CLI application"""
    parser = setup_argparse()
    args = parser.parse_args()
    
    # Load configuration
    config = load_config(args.config)
    
    # Initialize the system
    med_rag = AdaptiveMedRAG(config)
    
    # Handle commands
    if args.index:
        # Index documents
        success = med_rag.index_documents()
        if success:
            print("Documents indexed successfully")
        else:
            print("No documents found to index")
            
    elif args.query:
        # Answer medical query
        if args.user:
            response = med_rag.personalized_medical_query(args.query, args.user)
        else:
            response = med_rag.answer_medical_query(
                args.query, 
                strategy=args.strategy,
                explain=args.explain
            )
            
        # Print formatted response
        print("\n" + "=" * 80)
        print(f"QUERY: {response['query']}")
        print("-" * 80)
        print(f"ANSWER:\n{response['answer']}")
        print("-" * 80)
        print(f"RETRIEVAL: {response['retrieval']}")
        print(f"SOURCES: {', '.join(response['sources'][:3])}{'...' if len(response['sources']) > 3 else ''}")
        print("=" * 80 + "\n")
            
    elif args.evaluate:
        # Run evaluation
        evaluator = MedicalRAGEvaluator(med_rag)
        results = evaluator.evaluate_on_dataset(args.evaluate)
        
        # Print summary
        print("\n" + "=" * 80)
        print(f"EVALUATION RESULTS")
        print(f"Total questions: {results['total_questions']}")
        print(f"Overall accuracy: {results['accuracy']:.4f}")
        print("Strategy distribution:")
        for strategy, count in results['strategies'].items():
            print(f"  - {strategy}: {count} ({count/results['total_questions']*100:.1f}%)")
        print("=" * 80 + "\n")
        
    elif args.compare:
        # Load test queries
        if os.path.exists(args.compare):
            with open(args.compare, 'r') as f:
                test_queries = [line.strip() for line in f if line.strip()]
        else:
            test_queries = [args.compare]  # Use as a single query
            
        # Run comparison
        evaluator = MedicalRAGEvaluator(med_rag)
        results = evaluator.compare_strategies(test_queries)
        
        # Print summary
        print("\n" + "=" * 80)
        print(f"STRATEGY COMPARISON RESULTS")
        print(f"Queries tested: {results['queries']}")
        
        # Average similarity between strategies
        similarities = {}
        for query_result in results['strategy_comparison'].values():
            for pair, sim in query_result['cross_similarities'].items():
                if pair not in similarities:
                    similarities[pair] = []
                similarities[pair].append(sim)
        
        print("Average answer similarity between strategies:")
        for pair, sims in similarities.items():
            avg_sim = sum(sims) / len(sims)
            print(f"  - {pair}: {avg_sim:.4f}")
        print("=" * 80 + "\n")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()