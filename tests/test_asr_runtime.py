import importlib.util
from pathlib import Path
import tempfile
import unittest
import wave


@unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "ASR extra is not installed")
class ASRRuntimeTests(unittest.TestCase):
    def test_installed_decoder_reads_generated_wave_without_a_model(self):
        from faster_whisper.audio import decode_audio
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "input.media"
            with wave.open(str(media), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x00\x00" * 16_000)
            audio = decode_audio(str(media))
            self.assertEqual(audio.shape, (16_000,))
            self.assertEqual(str(audio.dtype), "float32")
            self.assertEqual(float(abs(audio).max()), 0)


if __name__ == "__main__":
    unittest.main()
