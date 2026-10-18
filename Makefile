.PHONY: setup dev test eval seed docker
setup:
	python -m pip install -r requirements.txt
dev:
	uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
test:
	python -m pytest backend/tests -q
eval:
	python eval/run_eval.py
seed:
	python scripts/seed_demo.py --data $(DATA)
docker:
	docker build -t machine-passport .
