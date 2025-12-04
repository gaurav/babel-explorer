import dataclasses
import functools
import requests

@dataclasses.dataclass
class Identifier:
    curie: str
    label: str = ""
    taxa: list[str] = dataclasses.field(default_factory=list)
    description: list[str] = dataclasses.field(default_factory=list)

    def __lt__(self, other):
        return self.curie < other.curie

    @staticmethod
    def from_dict(d: dict):
        identifier = Identifier(curie=d['identifier'])
        if 'label' in d:
            identifier.label = d['label']
        if 'taxa' in d:
            identifier.taxa = d['taxa']
        if 'description' in d:
            identifier.description = d['description']
        return identifier

class NodeNorm:
    def __init__(self, nodenorm_url: str=""):
        self.nodenorm_url = nodenorm_url

    @functools.lru_cache(maxsize=None)
    def normalize_curie(self, curie: str, conflate=True, drug_chemical_conflate=False, description=False, individual_types=None, include_taxa=None):
        response = requests.get(f"{self.nodenorm_url}get_normalized_nodes", params={
            "curie": curie,
            "conflate": conflate,
            "drug_chemical_conflate": drug_chemical_conflate,
            "description": description,
            "individual_types": individual_types,
            "include_taxa": include_taxa,
        })
        response.raise_for_status()
        result = response.json()

        return result[curie]

    @functools.lru_cache(maxsize=None)
    def get_clique_identifiers(self, curie, **kwargs):
        result = self.normalize_curie(curie, **kwargs)
        if 'equivalent_identifiers' not in result:
            return None
        return list(map(lambda x: Identifier.from_dict(x), result['equivalent_identifiers']))
