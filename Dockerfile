FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Copia os arquivos de definição de projeto e o código fonte
COPY pyproject.toml ./
COPY src/ ./src/

# Instala as dependências e o pacote
RUN pip install --no-cache-dir .

# Garante que a pasta de dados exista para o SQLite
RUN mkdir -p /app/data

# Expõe a porta do FastAPI
EXPOSE 8000

# Comando para iniciar o servidor uvicorn
CMD ["uvicorn", "ous_monitor.server:app", "--host", "0.0.0.0", "--port", "8000"]
