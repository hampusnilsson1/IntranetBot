# IntranetBot – AI Assistant for Falkenbergs Kommun's Intranet

**IntranetBot** is an AI assistant for Falkenbergs Kommun's intranet that helps employees find workplace information through simple chat interactions.

---

## 🚀 Features

- 🤖 Intelligent question answering powered by vector search and OpenAI.
- 🔁 Backend system for updating and maintaining the internal knowledge base.
- 🧠 Context-aware responses using embeddings from intranet content.
- 🌐 Frontend integration with Joomla modules using HTML, CSS, and JavaScript.

---

## 🏗️ Project Structure

### 🔙 Backend (Python)

- **API Server** – Handles employee queries and generates AI responses.
- **Embedding Generator** – Processes and updates vector database from intranet content.
- **Utilities** – Scripts for scraping, preprocessing, and updating internal data.

> Technologies: Python, Flask, Qdrant Vector DB, OpenAI API

### 🌐 Frontend (HTML/CSS/JS)

- Integrated into Joomla via custom module.
- Dynamic chatbot interface styled with pure CSS.
- Minimal dependencies to ensure compatibility with Joomla.

---

## 🧠 How It Works

1. **Employee types a question** in the IntranetBot chat window.
2. **Frontend sends request** to backend API.
3. **Backend searches** the vector database for relevant internal content.
4. **OpenAI generates a response** based on matched context.
5. **Answer is returned** to the employee in the chat interface.
