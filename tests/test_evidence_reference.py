from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from PIL import Image
TOOLS=Path(__file__).resolve().parents[1]/'plugins/ce-shared/tools'
sys.path.insert(0,str(TOOLS))
import ce_imagediff
import ce_console


class EvidenceReferenceTests(unittest.TestCase):
    def test_small_color_change_fails_rgb_reference_even_when_ssim_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a.png',Path(tmp)/'b.png'
            image=Image.new('RGB',(256,256),(25,25,25));image.save(a)
            image.putpixel((128,128),(255,0,0));image.save(b)
            legacy=ce_imagediff.compare(a,b,kind='world')
            strict=ce_imagediff.compare(a,b,kind='world',max_changed_percent=0)
            self.assertTrue(legacy['same']);self.assertFalse(strict['same'])
            self.assertTrue(strict['ssim_passed']);self.assertFalse(strict['pixels_passed'])
            self.assertTrue(ce_imagediff.compare(a,a,max_changed_percent=0)['same'])

    def test_region_uses_original_coordinates_and_rejects_invalid_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a.png',Path(tmp)/'b.png'
            im=Image.new('RGB',(64,64),'black');im.save(a);im.putpixel((50,50),(255,255,255));im.save(b)
            self.assertTrue(ce_imagediff.compare(a,b,max_changed_percent=0,region=(0,0,16,16))['same'])
            for params in ({'region':(63,0,2,1)},{'max_changed_percent':float('nan')},{'threshold':1.5},{'pixel_tolerance':True}):
                with self.assertRaises(ValueError):ce_imagediff.compare(a,b,**params)

    def test_profile_cli_emits_portable_viewer_and_explicit_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a,b=root/'a.png',root/'b.png'
            Image.new('RGB',(16,16),'blue').save(a);Image.new('RGB',(16,16),'red').save(b)
            profile=root/'reference.json';profile.write_text(json.dumps({'version':1,'max_changed_percent':0,'ssim':0}))
            viewer=root/'report.html'
            result=subprocess.run([sys.executable,str(TOOLS/'ce_imagediff.py'),str(a),str(b),'--reference',str(profile),'--viewer',str(viewer),'--json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertFalse(json.loads(result.stdout)[0]['same'])
            text=viewer.read_text();self.assertIn('data:image/png;base64,',text);self.assertIn('type="range"',text)
            self.assertNotIn('file://',text)
            profile.write_text('{"version":1,"version":1,"max_changed_percent":0}')
            invalid=subprocess.run([sys.executable,str(TOOLS/'ce_imagediff.py'),str(a),str(b),'--reference',str(profile)],capture_output=True,text=True)
            self.assertEqual(invalid.returncode,2)


    def test_reference_requires_real_numeric_pixel_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'a.png'; Image.new('RGB',(8,8),'blue').save(image)
            profile=root/'r.json'
            for value in (None,True,'0',-1,101):
                with self.subTest(value=value):
                    profile.write_text(json.dumps({'version':1,'max_changed_percent':value}))
                    run=subprocess.run([sys.executable,str(TOOLS/'ce_imagediff.py'),str(image),str(image),'--reference',str(profile)],capture_output=True,text=True)
                    self.assertEqual(run.returncode,2,run.stdout)

    def test_output_collisions_preserve_all_inputs_and_reject_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'a.png'; Image.new('RGB',(8,8),'blue').save(image)
            clip=root/'clip.mp4';clip.write_bytes(b'original clip')
            profile=root/'r.json';profile.write_text('{"version":1,"max_changed_percent":0}')
            alias=root/'alias.mp4';alias.hardlink_to(clip)
            cases=[['--viewer',str(clip),'--clip',str(clip)],
                   ['--viewer',str(alias),'--clip',str(clip)],
                   ['--out',str(profile),'--reference',str(profile)],
                   ['--viewer',str(root/'same.png'),'--out',str(root/'same.png')],
                   ['--viewer',str(image)]]
            original={p:p.read_bytes() for p in (image,clip,profile)}
            for args in cases:
                with self.subTest(args=args):
                    run=subprocess.run([sys.executable,str(TOOLS/'ce_imagediff.py'),str(image),str(image),*args],capture_output=True,text=True)
                    self.assertEqual(run.returncode,2,run.stdout)
                    for path,data in original.items():self.assertEqual(path.read_bytes(),data)
            with self.assertRaises(ValueError):ce_console.evidence({'same':True},image,image,clip,clip)


if __name__=='__main__':unittest.main()
