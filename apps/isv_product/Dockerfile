# isv_product service image (M6). Built by the platform deployer from the
# merged commit as isv-product:<sha7>; k3s on Rancher Desktop shares the moby
# image store, so no registry push is needed on the lab stand.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "isv_product.main:app", "--host", "0.0.0.0", "--port", "8000"]
