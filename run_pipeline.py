"""
Main entry point. Run individual steps or the full pipeline.

Usage:
  python run_pipeline.py --step 1          # filter & build manifest
  python run_pipeline.py --step 2          # download documents
  python run_pipeline.py --step 3          # validate PDFs
  python run_pipeline.py --step 4          # extract structured data (Claude Batch API)
  python run_pipeline.py --step 1 2 3 4   # run multiple steps
  python run_pipeline.py --all             # run all steps

Step 4 requires ANTHROPIC_API_KEY to be set in the environment.
It submits a batch to the Claude Batch API and polls until complete — this
can take minutes to hours depending on corpus size. The batch ID is saved to
output/extraction_batch_id.txt so an interrupted run can be resumed by
re-running --step 4.
"""
import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='RERA Construction Certificate Parser Pipeline')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--step', nargs='+', type=int, choices=[1, 2, 3, 4], metavar='N', help='Run step(s)')
    group.add_argument('--all', action='store_true', help='Run all steps')
    args = parser.parse_args()

    steps = [1, 2, 3, 4] if args.all else sorted(set(args.step))

    manifest = None
    for step in steps:
        logger.info('=' * 60)
        logger.info('Running step %d', step)
        logger.info('=' * 60)
        if step == 1:
            from pipeline.step01_filter import run
            manifest = run()
        elif step == 2:
            from pipeline.step02_download import run
            run(manifest)
        elif step == 3:
            from pipeline.step03_validate import run
            run(manifest)
        elif step == 4:
            from pipeline.step04_extract import run
            run()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )
    main()
