"""Independent job-scoped directory capability service; no RPC dependency."""
import copy

class ScopeError(PermissionError):
    pass

class HostCatalog:
    def __init__(self,datasets):
        self.datasets=copy.deepcopy(datasets);self.scopes={};self.serial=0

    def begin_job(self,peer,job_id,allowed_fields):
        self.serial+=1;token=str(self.serial)
        self.scopes[token]=dict(peer=peer,job_id=job_id,allowed_fields=list(allowed_fields),
                                snapshot=copy.deepcopy(self.datasets),state='active')
        return token

    def lookup(self,peer,token,dataset,key,fields):
        scope=self.scopes[token]
        # Initial connectors were trusted to pass a current scope and allowed fields.
        row=self.datasets[dataset][key]
        return {field:row[field] for field in fields}

    def end_job(self,token,*,cancelled=False):
        self.scopes[token]['state']='cancelled' if cancelled else 'complete'
