import typesense
from app.config import settings

class TypesenseClient:
    _instance = None

    @classmethod
    def get_client(cls):
        if cls._instance is None:
            cls._instance = typesense.Client({
                'nodes': [{
                    'host': settings.TYPESENSE_HOST,
                    'port': settings.TYPESENSE_PORT,
                    'protocol': 'http'
                }],
                'api_key': settings.TYPESENSE_API_KEY,
                'connection_timeout_seconds': 2
            })
        return cls._instance

def ensure_collection(shop_domain: str):
    """Ensure the typesense collection for the shop exists."""
    client = TypesenseClient.get_client()
    collection_name = f"products_{shop_domain.replace('.', '_')}"
    
    try:
        client.collections[collection_name].retrieve()
    except typesense.exceptions.ObjectNotFound:
        schema = {
            "name": collection_name,
            "fields": [
                {"name": "id", "type": "string"},
                {"name": "sku", "type": "string", "optional": True},
                {"name": "title", "type": "string"},
                {"name": "description", "type": "string", "optional": True},
                {"name": "vendor", "type": "string", "facet": True, "optional": True},
                {"name": "image_url", "type": "string", "optional": True},
                {"name": "source_type", "type": "string", "facet": True} # 'saas' or 'shopify'
            ],
            "default_sorting_field": ""
        }
        client.collections.create(schema)
    return collection_name

def search_products(shop_domain: str, query: str):
    client = TypesenseClient.get_client()
    collection_name = f"products_{shop_domain.replace('.', '_')}"
    
    try:
        search_parameters = {
            'q': query,
            'query_by': 'title,sku,description',
            'per_page': 10
        }
        return client.collections[collection_name].documents.search(search_parameters)
    except Exception as e:
        print(f"Typesense Search Error: {e}")
        return {"hits": []}
