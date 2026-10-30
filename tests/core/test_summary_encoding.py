"""The stored summary encoding. Fixtures are synthetic; both pinned payloads are real output."""
import base64
from pathlib import Path
import re
import unittest
import zlib

import machine_audit
from machine_audit.derive import rollups,points
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.store import encode,pack_summaries,unpack_summaries
from machine_audit.summary_dictionary import SUMMARY_DICTIONARY

S=10**9
ROW=dict(above_s={},additive=False,aggregation='sample-mean',component='memory',count=2,covered_s=0.0,covered_windows={},end_ns=20,entity='process:1',
         histogram_s=[],instance_count=1,kind='gauge',label='p1',last=7.0,last_ns=20,max=7.0,max_ns=20,mean=6.0,members=['process:1'],method='observed-windows-v1',
         min=5.0,partial_boundary=False,partial_members=False,peak_ns=None,peak_rate=None,rate_unit='bytes/s',sample_count=2,scale=1.0,scope='process',
         signal='process.phys_footprint',sleep_s=0.0,source='synthetic.process',start_ns=10,status='observed',statuses={'observed':2},sum=12.0,total=None,unit='bytes',value=None)
# ROW as the release before the dictionary saved it: zlib level 1, no dictionary.
BEFORE=('eAFNUttuwyAM/ReeaddE2ibtV6IqcoibonETONmqqf8+G9J1ecAXzjk+Fhl6PQyDgiluOBalFcyzJbuhpMuScQGyMXBlok8xYKCar3vcMONciYYVJP+yYY5fIoVhHkNLyNKNO1dbKC4Z'
        'fGXYUAiCwdHEJvfJVEY5mNDVWGSYg0JNx8M313zuJYIY8+gnzDLHI12jKHgrFwkyWXDjxPIzZDHwaD05CeGzydUsA8nqEsY1WJlfwCf3NFkMOIEUE1ONdgkgdotDTHWxEtds6h2xhabOq9IqJluCNV09NyhS5e/jNnArqjM/'
        'y89dX8AVfFg4+L+Fo2zT69PxpBnVn3izHA2W8tEpPZx1p9UC6yIeEnfeGcegPbxxNfwjnLWKU8G84XzYX++wMeuVcc1AO8PqnK6Hmm6E5YV36HXHqIeYjGs+jul6K+MlRkrZyr8iVlW5BboiWXPcYUp33H5MV/rnmX/0d931zPo3'
        'UdXiLN8v/E7nVA==')
# ROW as this release saves it. Changing the dictionary orphans every saved bundle: this
# fails first, and the bytes must stay.
WITH_DICTIONARY='eLt4iT53gxBDPLaGZwwRXGVkBKsljJCqCUO09TRKBUARc6A6oCIoZQakopE04KoDTIHqCM2RGZG0tqi4Mq8kIxVYviMqBUOctYJRrY4hsBpFthFaKYAAAPxO51Q='


class SummaryEncodingTests(unittest.TestCase):
    def test_bundles_saved_before_and_with_the_dictionary_both_decode(self):
        # Hour rows outlive releases: Big Mac holds days of the older form, and a dictionary
        # change would make everything saved with it unreadable.
        for payload in (BEFORE,WITH_DICTIONARY):self.assertEqual(unpack_summaries(base64.b64decode(payload)),[ROW])

    def test_a_cut_bundle_is_damage_not_a_short_answer(self):
        # Cut inside the checksum, the JSON is whole: only the stream end shows the damage.
        with self.assertRaises(ValueError):unpack_summaries(base64.b64decode(WITH_DICTIONARY)[:-2])

    def test_real_process_rows_round_trip_and_shrink(self):
        # A process's hour of rows, from the real derivation. Without the dictionary reaching
        # the compressor a short bundle saves little.
        records=[]
        for n in range(30):
            c=Clock(n*10*S,n*10*S,n*10*S)
            kw=dict(entity='process:boot:7:9',label='p7',scope='process')
            fields=(Field('process.user_time',float(n),'seconds','counter',additive=True,rate_unit='CPUs',**kw),
                    Field('process.system_time',n/2,'seconds','counter',additive=True,rate_unit='CPUs',**kw),
                    Field('process.pageins',n,'pages','counter',additive=True,**kw),
                    Field('process.phys_footprint',1000+n,'bytes',component='memory',**kw))
            records.append(Record(Reading('macos.process','cpu',c,c,fields),'boot','run',n+1,c.wall_ns,c))
        rows=[row for _,row in rollups(points(records),3600*S)]
        packed=pack_summaries(rows)
        self.assertEqual(sorted(unpack_summaries(packed),key=lambda r:(r['signal'],r['entity'])),sorted(rows,key=lambda r:(r['signal'],r['entity'])))
        groups={}
        for row in rows:groups.setdefault(tuple(sorted(row)),[]).append([row[k] for k in sorted(row)])
        before=zlib.compress(encode([2,[[keys,values] for keys,values in groups.items()]]).encode(),1)
        self.assertLess(len(packed),len(before)*.75)

    def test_the_dictionary_holds_only_names_from_the_code(self):
        # privacy.md: a dictionary sits in every install and every source copy. One trained on
        # recorded rows would carry program labels and ids. Every word of a name must occur in the
        # code, and nothing may look like an id, a time or a measured value.
        package=Path(machine_audit.__file__).parent
        code=set()
        for path in package.rglob('*.py'):
            if path.name!='summary_dictionary.py':code.update(re.findall(r'[A-Za-z]+',path.read_text()))
        text=SUMMARY_DICTIONARY.decode('ascii')
        self.assertEqual(set(re.findall(r'[A-Za-z]+',text))-code,set())
        self.assertIsNone(re.search(r'[0-9]{3,}|[0-9a-f]{8}-[0-9a-f]{4}|process:[^"]|program:',text))
