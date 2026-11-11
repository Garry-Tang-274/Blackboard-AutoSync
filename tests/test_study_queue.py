from pathlib import Path
import tempfile
import unittest
import bb_sync as bb
from study_queue import StudyQueue

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bb.write_json(self.root / 'config.json', dict(base_url='https://example.org', download_dir='files', course_selection_mode='all'))
        self.cfg = bb.Config.load(self.root / 'config.json')
        self.course = bb.Course('_1_1', 'Test', 'https://example.org/')
        self.link = bb.Link('https://example.org/bbcswebdav/file.pdf')
        self.queue = StudyQueue(self.root)
    def tearDown(self):
        self.temp.cleanup()
    def save(self, data=b'%PDF-source', name='file.pdf', folder='Lecture'):
        archive = bb.Archive(self.root, self.cfg)
        try:
            archive.save_file(self.course, (folder,), self.link, name, data, {})
        finally:
            archive.close()
    def test_existing_files_are_baseline_and_init_is_idempotent(self):
        self.save()
        self.assertEqual(self.queue.init()['known'], 1)
        self.assertFalse(self.queue.init()['initialized'])
        self.assertEqual(self.queue.scan()['added'], 0)
    def test_new_file_is_queued_once(self):
        self.queue.init()
        self.save()
        self.assertEqual(self.queue.scan()['added'], 1)
        self.assertEqual(self.queue.scan()['added'], 0)
    def test_content_update_supersedes_unsent_source(self):
        self.queue.init()
        self.save()
        self.queue.scan()
        self.save(b'%PDF-new')
        self.assertEqual(self.queue.scan()['added'], 1)
        self.assertEqual([x['status'] for x in self.queue.read()['jobs'].values()].count('pending'), 1)
    def test_duplicate_placement_does_not_duplicate_job(self):
        self.queue.init()
        self.save()
        self.save(folder='Other')
        pending = self.queue.scan()['pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0]['paths']), 2)
    def test_sent_is_not_completed_and_receipt_checks_pdf(self):
        self.queue.init()
        self.save()
        ids = [x['id'] for x in self.queue.scan()['pending']]
        batch = self.queue.batch('lecture', ids)
        self.queue.mark(batch['batch_id'], 'sending')
        self.queue.mark(batch['batch_id'], 'sent')
        self.assertEqual(self.queue.reconcile()['verified_completed'], [])
        item = self.queue.read()['batches'][batch['batch_id']]
        pdf = self.root / 'files' / 'study.pdf'
        pdf.write_bytes(b'%PDF-test result')
        bb.write_json(Path(item['receipt_path']), dict(batch_id=batch['batch_id'], status='completed', source_ids=ids,
                                                     visual_qa_passed=True, outputs=[dict(path=str(pdf), sha256=bb.digest(pdf.read_bytes()))]))
        self.assertEqual(self.queue.reconcile()['verified_completed'], [])
        result = __import__('json').loads(Path(item['receipt_path']).read_text())
        result['drive_outputs'] = [dict(file_id='pdf-id', folder_id='unit-folder', url='https://drive.google.com/file/d/pdf-id/view')]
        bb.write_json(Path(item['receipt_path']), result)
        self.assertEqual(self.queue.reconcile()['awaiting_drive_verification'], [batch['batch_id']])
        proof = dict(batch_id=batch['batch_id'], source_ids=ids, checked_at=bb.now(),
                     method='google_drive.get_file_metadata', outputs=[dict(file_id='pdf-id',
                     file_metadata=dict(id='pdf-id', mimeType='application/pdf', parents=['unit-folder'], size=16, trashed=False),
                     folder_metadata=dict(id='unit-folder', mimeType='application/vnd.google-apps.folder', trashed=False))])
        bb.write_json(self.queue.directory / 'drive-verifications' / (batch['batch_id'] + '.json'), proof)
        self.assertEqual(self.queue.reconcile()['verified_completed'], [batch['batch_id']])
        self.assertEqual(self.queue.read()['batches'][batch['batch_id']]['drive_outputs'], result['drive_outputs'])

    def test_drive_archive_needs_matching_parent_and_can_use_external_qa_cache(self):
        self.queue.init()
        self.save()
        ids = [x['id'] for x in self.queue.scan()['pending']]
        batch = self.queue.batch('lecture', ids)
        item = self.queue.read()['batches'][batch['batch_id']]
        pdf = self.root / 'qa-cache.pdf'
        pdf.write_bytes(b'%PDF-test result')
        bb.write_json(Path(item['receipt_path']), dict(batch_id=batch['batch_id'], status='completed', source_ids=ids,
                      visual_qa_passed=True, outputs=[dict(path=str(pdf), sha256=bb.digest(pdf.read_bytes()))],
                      drive_outputs=[dict(file_id='pdf-id', folder_id='unit-folder', url='https://drive.google.com/file/d/pdf-id/view')]))
        proof = dict(batch_id=batch['batch_id'], source_ids=ids, checked_at=bb.now(), method='google_drive.get_file_metadata',
                     outputs=[dict(file_id='pdf-id', file_metadata=dict(id='pdf-id', mimeType='application/pdf',
                     parents=['wrong-folder'], size=16, trashed=False),
                     folder_metadata=dict(id='unit-folder', mimeType='application/vnd.google-apps.folder', trashed=False))])
        proof_path = self.queue.directory / 'drive-verifications' / (batch['batch_id'] + '.json')
        bb.write_json(proof_path, proof)
        with self.assertRaises(ValueError): self.queue.reconcile()
        proof['outputs'][0]['file_metadata']['parents'] = ['unit-folder']
        bb.write_json(proof_path, proof)
        self.assertEqual(self.queue.reconcile()['verified_completed'], [batch['batch_id']])
    def test_mismatched_source_receipt_is_not_completed(self):
        self.queue.init()
        self.save()
        ids = [x['id'] for x in self.queue.scan()['pending']]
        batch = self.queue.batch('guidance', ids)
        item = self.queue.read()['batches'][batch['batch_id']]
        bb.write_json(Path(item['receipt_path']), dict(batch_id=batch['batch_id'], status='completed', source_ids=['wrong']))
        with self.assertRaises(ValueError): self.queue.reconcile()
        self.assertEqual(self.queue.read()['batches'][batch['batch_id']]['status'], 'prepared')
    def test_corrupt_source_is_not_queued(self):
        self.queue.init()
        self.save()
        record = next(iter(self.queue.current().values()))
        Path(record['paths'][0]).write_bytes(b'corrupt')
        self.assertEqual(self.queue.scan()['added'], 0)
