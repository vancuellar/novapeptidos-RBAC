import requests
import sys
import os
from datetime import datetime

BASE_URL = os.environ.get("NOVA_API_BASE_URL", "http://localhost:8001/api")

class NovaPeptidesAPITester:
    def __init__(self):
        self.base_url = BASE_URL
        self.admin_token = None
        self.customer_token = None
        self.tests_run = 0
        self.tests_passed = 0
        self.test_product_id = None
        self.test_order_id = None
        self.test_order_number = None

    def log(self, emoji, message):
        print(f"{emoji} {message}")

    def run_test(self, name, method, endpoint, expected_status, data=None, token=None, params=None):
        """Run a single API test"""
        url = f"{self.base_url}{endpoint}"
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        self.tests_run += 1
        self.log("🔍", f"Testing {name}...")
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=data, headers=headers, timeout=10)
            elif method == 'PUT':
                response = requests.put(url, json=data, headers=headers, timeout=10)
            elif method == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=10)

            success = response.status_code == expected_status
            if success:
                self.tests_passed += 1
                self.log("✅", f"Passed - Status: {response.status_code}")
                try:
                    return True, response.json()
                except:
                    return True, {}
            else:
                self.log("❌", f"Failed - Expected {expected_status}, got {response.status_code}")
                try:
                    self.log("📄", f"Response: {response.json()}")
                except:
                    self.log("📄", f"Response: {response.text[:200]}")
                return False, {}

        except Exception as e:
            self.log("❌", f"Failed - Error: {str(e)}")
            return False, {}

    def test_auth(self):
        """Test authentication endpoints"""
        self.log("🔐", "\n=== Testing Authentication ===")
        
        # Test admin login
        success, response = self.run_test(
            "Admin Login",
            "POST",
            "/auth/login",
            200,
            data={"email": "admin@novapeptides.mx", "password": "Admin123!"}
        )
        if success and 'token' in response:
            self.admin_token = response['token']
            self.log("🎫", f"Admin token obtained")
        
        # Test customer login
        success, response = self.run_test(
            "Customer Login",
            "POST",
            "/auth/login",
            200,
            data={"email": "cliente@novapeptides.mx", "password": "Cliente123!"}
        )
        if success and 'token' in response:
            self.customer_token = response['token']
            self.log("🎫", f"Customer token obtained")
        
        # Test /auth/me with customer token
        self.run_test(
            "Get Current User (Customer)",
            "GET",
            "/auth/me",
            200,
            token=self.customer_token
        )
        
        # Test register new user
        test_email = f"test_{datetime.now().strftime('%H%M%S')}@test.com"
        self.run_test(
            "Register New User",
            "POST",
            "/auth/register",
            200,
            data={"name": "Test User", "email": test_email, "password": "Test123!"}
        )

    def test_categories(self):
        """Test categories endpoint"""
        self.log("📂", "\n=== Testing Categories ===")
        
        success, response = self.run_test(
            "Get Categories",
            "GET",
            "/categories",
            200
        )
        if success and isinstance(response, list):
            self.log("📊", f"Found {len(response)} categories")
            if len(response) >= 8:
                self.log("✅", "Expected 8 categories found")
            else:
                self.log("⚠️", f"Expected 8 categories, found {len(response)}")

    def test_products(self):
        """Test product endpoints"""
        self.log("🧪", "\n=== Testing Products ===")
        
        # Test list all products
        success, response = self.run_test(
            "Get All Products",
            "GET",
            "/products",
            200
        )
        if success and isinstance(response, list):
            self.log("📊", f"Found {len(response)} products")
            if len(response) > 0:
                self.test_product_id = response[0].get('id')
                product_slug = response[0].get('slug')
                self.log("🆔", f"Test product ID: {self.test_product_id}, slug: {product_slug}")
                
                # Test get product by slug
                if product_slug:
                    self.run_test(
                        "Get Product by Slug",
                        "GET",
                        f"/products/{product_slug}",
                        200
                    )
        
        # Test product filters
        self.run_test(
            "Filter Products by Category",
            "GET",
            "/products",
            200,
            params={"category": "recuperacion"}
        )
        
        self.run_test(
            "Search Products",
            "GET",
            "/products",
            200,
            params={"search": "BPC"}
        )
        
        self.run_test(
            "Filter In-Stock Products",
            "GET",
            "/products",
            200,
            params={"in_stock": True}
        )
        
        self.run_test(
            "Filter by Max Price",
            "GET",
            "/products",
            200,
            params={"max_price": 1000}
        )
        
        self.run_test(
            "Sort Products by Price Ascending",
            "GET",
            "/products",
            200,
            params={"sort": "price_asc"}
        )

    def test_orders(self):
        """Test order endpoints"""
        self.log("📦", "\n=== Testing Orders ===")
        
        # Create order as guest
        order_data = {
            "items": [
                {
                    "product_id": self.test_product_id or "test-id",
                    "name": "Test Product",
                    "price": 850,
                    "quantity": 2,
                    "presentation": "10 mg / vial",
                    "image_url": ""
                }
            ],
            "customer": {
                "full_name": "Test Customer",
                "email": "test@test.com",
                "phone": "5512345678",
                "address": "Test Address 123",
                "city": "CDMX",
                "state": "CDMX",
                "postal_code": "01000",
                "notes": "Test order"
            },
            "payment_method": "mercado_pago",
            "shipping": 199
        }
        
        success, response = self.run_test(
            "Create Order (Guest)",
            "POST",
            "/orders",
            200,
            data=order_data
        )
        if success:
            self.test_order_id = response.get('id')
            self.test_order_number = response.get('order_number')
            self.log("🆔", f"Test order ID: {self.test_order_id}, number: {self.test_order_number}")
            
            # Test get order by number
            if self.test_order_number:
                self.run_test(
                    "Get Order by Number",
                    "GET",
                    f"/orders/{self.test_order_number}",
                    200
                )
        
        # Test get my orders (authenticated)
        self.run_test(
            "Get My Orders (Customer)",
            "GET",
            "/orders/me",
            200,
            token=self.customer_token
        )

    def test_admin_products(self):
        """Test admin product endpoints"""
        self.log("👑", "\n=== Testing Admin Product Endpoints ===")
        
        # Test create product (admin)
        new_product = {
            "name": "Test Peptide",
            "slug": f"test-peptide-{datetime.now().strftime('%H%M%S')}",
            "category": "recuperacion",
            "short_description": "Test peptide for testing",
            "description": "This is a test peptide",
            "presentation": "10 mg / vial",
            "form": "Liofilizado",
            "purity": "99%",
            "price": 999,
            "stock": 10,
            "image_url": "https://via.placeholder.com/400",
            "featured": False,
            "is_new": True
        }
        
        success, response = self.run_test(
            "Create Product (Admin)",
            "POST",
            "/admin/products",
            200,
            data=new_product,
            token=self.admin_token
        )
        
        created_product_id = None
        if success:
            created_product_id = response.get('id')
            self.log("🆔", f"Created product ID: {created_product_id}")
        
        # Test create product without admin token (should fail)
        self.run_test(
            "Create Product (Non-Admin) - Should Fail",
            "POST",
            "/admin/products",
            403,
            data=new_product,
            token=self.customer_token
        )
        
        # Test update product
        if created_product_id:
            self.run_test(
                "Update Product (Admin)",
                "PUT",
                f"/admin/products/{created_product_id}",
                200,
                data={"price": 1099, "stock": 15},
                token=self.admin_token
            )
            
            # Test delete product
            self.run_test(
                "Delete Product (Admin)",
                "DELETE",
                f"/admin/products/{created_product_id}",
                200,
                token=self.admin_token
            )

    def test_admin_orders(self):
        """Test admin order endpoints"""
        self.log("👑", "\n=== Testing Admin Order Endpoints ===")
        
        # Test get all orders (admin)
        self.run_test(
            "Get All Orders (Admin)",
            "GET",
            "/admin/orders",
            200,
            token=self.admin_token
        )
        
        # Test get all orders (non-admin) - should fail
        self.run_test(
            "Get All Orders (Non-Admin) - Should Fail",
            "GET",
            "/admin/orders",
            403,
            token=self.customer_token
        )
        
        # Test update order status
        if self.test_order_id:
            self.run_test(
                "Update Order Status (Admin)",
                "PUT",
                f"/admin/orders/{self.test_order_id}/status",
                200,
                data={"status": "confirmado"},
                token=self.admin_token
            )
        
        # Test admin stats
        self.run_test(
            "Get Admin Stats",
            "GET",
            "/admin/stats",
            200,
            token=self.admin_token
        )

    def test_ai_chat(self):
        """Test AI chat endpoint"""
        self.log("🤖", "\n=== Testing AI Chat ===")
        
        # Test AI chat streaming
        session_id = f"test-session-{datetime.now().strftime('%H%M%S')}"
        self.log("🔍", f"Testing AI Chat Stream...")
        self.tests_run += 1
        
        try:
            url = f"{self.base_url}/ai/chat"
            response = requests.post(
                url,
                json={"session_id": session_id, "message": "¿Qué es BPC-157?"},
                headers={'Content-Type': 'application/json'},
                stream=True,
                timeout=30
            )
            
            if response.status_code == 200:
                # Read streaming response
                content = ""
                for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
                    if chunk:
                        content += chunk
                        if len(content) > 100:  # Stop after getting some content
                            break
                
                if len(content) > 0:
                    self.tests_passed += 1
                    self.log("✅", f"AI Chat Stream working - received {len(content)} chars")
                    self.log("📄", f"Sample: {content[:100]}...")
                else:
                    self.log("❌", "AI Chat returned empty response")
            else:
                self.log("❌", f"AI Chat failed - Status: {response.status_code}")
        except Exception as e:
            self.log("❌", f"AI Chat error: {str(e)}")
        
        # Test get chat history
        self.run_test(
            "Get Chat History",
            "GET",
            f"/ai/history/{session_id}",
            200
        )

    def run_all_tests(self):
        """Run all tests"""
        self.log("🚀", "Starting Nova Peptides API Tests")
        self.log("🌐", f"Base URL: {self.base_url}")
        
        # Test health endpoint
        self.run_test("Health Check", "GET", "/", 200)
        
        # Run test suites
        self.test_auth()
        self.test_categories()
        self.test_products()
        self.test_orders()
        self.test_admin_products()
        self.test_admin_orders()
        self.test_ai_chat()
        
        # Print summary
        self.log("📊", f"\n{'='*50}")
        self.log("📊", f"Tests passed: {self.tests_passed}/{self.tests_run}")
        success_rate = (self.tests_passed / self.tests_run * 100) if self.tests_run > 0 else 0
        self.log("📊", f"Success rate: {success_rate:.1f}%")
        self.log("📊", f"{'='*50}")
        
        return 0 if self.tests_passed == self.tests_run else 1

def main():
    tester = NovaPeptidesAPITester()
    return tester.run_all_tests()

if __name__ == "__main__":
    sys.exit(main())
