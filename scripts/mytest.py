'''
Comments
'''
import os
import sys
import django
#proj_path='/var/www/disturbance'
#proj_path='/app'
proj_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(proj_path)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "disturbance.settings")
django.setup()


from disturbance.components.proposals.models import Proposal

p=Proposal.objects.last()

#print(p.__dict__)
print(p)
print(f'BASE_DIR: {proj_path}')
