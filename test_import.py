import sys
print(f"--- Python Executable: {sys.executable}")
print(f"--- Python Version: {sys.version}")
print(f"--- sys.path: {sys.path}")
try:
    import deepseek
    # veya daha spesifik olarak:
    # from deepseek import DeepSeekClient
    print("--- !!! Deepseek başarıyla import edildi !!! ---")
except ImportError:
    print("--- !!! Deepseek import edilirken ImportError alındı !!! ---")
except Exception as e:
    import traceback
    print(f"--- !!! Deepseek import edilirken BAŞKA BİR HATA alındı: {type(e).__name__}: {e} !!! ---")
    print("--- Traceback ---")
    traceback.print_exc()
    print("--- End Traceback ---")

print("--- Test script bitti ---")