# Updated by Tal Franji at 2025-11-03 11:20
from typing import Optional, List, Union
import base64
import os
import serial  # https://pypi.org/project/pyserial/
import sys
import random
import re
import requests
import time
import itertools

import serial.tools.list_ports

class DummySerial:
    """Mock class for serial.Serial to simulate LoRa modem behavior."""
    def __init__(self, device: str, speed: int = 115200):
        self.is_open = True
    def write(self, data: bytes) -> None:
        pass # ignore
    
    def read(self, size: int) -> bytes:
        return None
    
    def close(self) -> None:
        self.is_open = False


class LoRaModem:
    """Class to handle LoRa modem communication via serial port."""
    
    def __init__(self, device: str, speed: int = 115200):
        """
        Initialize the LoRa modem.
        
        Args:
            device: Serial device path or index number
            speed: Baud rate for serial communication
        """
        self.port: Optional[Union[serial.Serial, DummySerial]] = None
        self.simulator_url: Optional[str] = None
        self.simulator_recv: List[dict] = list()
        self.simulator_seen_seq = set()  # prevents me from recieving messages I have sent
        self.in_at_mode: bool = False  # is the model in AT command mode
        self.open_port(device, speed)
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup."""
        self.close()
    
    def close(self):
        """Close the serial port."""
        if self.port and self.port.is_open:
            self.at_mode_leave()
            self.port.close()
            self.port = None
    
    def _find_serial_port_by_index(self, port_index: int, pattern: str = r"usb(modem|serial)") -> str:
        """Find serial port by index matching pattern."""
        
        ports = serial.tools.list_ports.comports()
        ports_sorted = list(sorted(port.device for port in ports if re.search(pattern, port.device)))
        if not ports_sorted:
            raise RuntimeError("No serial devices found")
        if port_index >= len(ports_sorted):
            raise RuntimeError(f"Port index {port_index} out of range (0-{len(ports_sorted)-1})")
        return ports_sorted[port_index]
    
    def open_port(self, device: str, speed: int = 115200) -> None:
        """
        Open serial port connection.
        
        Args:
            device: Serial device path or index number as string
            speed: Baud rate
        """
        port_name = device
        if device == "dummy":
            self.port = DummySerial(device, speed)
            return
        if device == "sim":
            device = "http://34.165.8.95:8080"  # Franji's Google cloud node 2025-11-4

        if re.match(r"https?://", device):
            self.simulator_url = device
            print(f"Using Simulator server (at {self.simulator_url}) instead of LoRa Modem")
            return
        
        # Check if device is just a number (port index)
        if re.match(r"^\d+$", device):
            port_index = int(device) - 1
            port_name = self._find_serial_port_by_index(port_index)
        
        self.port = serial.Serial(port_name, speed, timeout=0.1)
    
    def _write_crlf(self, s: str) -> None:
        """Write string to serial port with CRLF."""
        if not s.endswith("\r\n"):
            if s.endswith("\n"):
                s = s[:-1]
            s += "\r\n"
        self.port.write(s.encode("ascii"))
    
    def _read_delim(self, ends_with: str = "\n") -> Optional[str]:
        """Read from serial port until delimiter."""
        recv = ""
        while True:
            b = self.port.read(1)
            if not b:
                return None
            char_code = b[0]
            if (char_code < 32 or char_code > 127) and char_code not in [10, 13]:
                print("WARNING - suspicious char from LoRa modem: ", char_code, "%02X" % char_code)
            c = b.decode("ascii", "ignore")
            recv += c
            if recv.endswith(ends_with):
                return recv
    
    def _read_till_ok_error(self) -> List[str]:
        """Read from the serial port until we get 'OK' or 'ERROR'."""
        resp = []
        while True:
            line = self._read_delim()
            if not line:
                print("Waiting for OK/ERROR")
                time.sleep(0.1)
                continue
            line = line.strip()  # remove any trailing whitespace - mostly "\r"
            resp.append(line)
            if line in ["OK", "ERROR"]:
                return resp
    
    def at_mode_enter(self) -> None:
        """Enter AT command mode."""
        if self.in_at_mode:
            return
        self.in_at_mode = True
        self._write_crlf("+++")
        time.sleep(1.1)  # this is part of the AT command mode enter sequence
    
    def at_mode_leave(self) -> None:
        """Leave AT command mode."""
        if not self.in_at_mode:
            return
        self.in_at_mode = False
        self._write_crlf("AT+EXIT")
        # after EXIT we get "OK" with no CRLF
        self._read_delim(ends_with="OK")
    
    def at_command(self, cmd: str, fail_on_error: bool = True, print_output: bool = True) -> List[str]:
        """
        Execute AT command.
        
        Args:
            cmd: AT command to execute
            fail_on_error: Raise exception on ERROR response
            print_output: Print command response
            
        Returns:
            List of response lines
        """
        self.at_mode_enter()
        self._write_crlf(cmd)
        resp = self._read_till_ok_error()
        
        if print_output:
            if resp:
                print("\n".join(resp))
            else:
                print("NO RESPONSE")
        
        if fail_on_error and resp[-1] != "OK":
            raise RuntimeError(f"Command failed: {cmd} - response: {resp}")
        
        return resp
    
    def send_to_simulator(self, data: Optional[bytes]):
        if data:
            data_u64 = base64.urlsafe_b64encode(data)
            seq = random.randint(1, 1_000_000)
            params = dict(p=data_u64, s=seq)
            self.simulator_seen_seq.add(seq)
        else:
            params = None
        while True:
            response = requests.get(self.simulator_url, params=params)
            if response.status_code != 429:
                break
            # reached rate limit
            time.sleep(0.1)

        if response.status_code != 200:
            print(f"cannot connect to simulator at {self.simulator_url}")
            return
        try:
            jobj = response.json()
            # assume it is a JSON has 'payloads' array
            for item in jobj.get('payloads', []):
                seq = item.get('seq')
                data = item.get('data')
                if not isinstance(data, str):
                    continue
                if any(seq == item.get('seq') for item in self.simulator_recv):
                    continue  # ignore already recieved.
                self.simulator_recv.append(item)
        except:
            pass  # ingnore errors 
        # limit memory used to keep history to 1000
        self.simulator_recv = self.simulator_recv[-1000:]

    def read_from_simulator(self) -> Optional[bytes]:
        self.send_to_simulator(None)
        if not self.simulator_recv:
            return None
        last = self.simulator_recv.pop(0)
        seq = last.get('seq')
        if seq and seq in self.simulator_seen_seq:
            return None
        self.simulator_seen_seq.add(seq)
        payload_b64 = last.get('data')
        if not payload_b64:
            return None  # this is a bug - TODO(franji): give error?
        data = base64.urlsafe_b64decode(payload_b64)
        return data

    def write_packet(self, packet: List[int]) -> None:
        """
        Write packet in packet mode.
        
        Args:
            packet: List of bytes to send
        """
        if self.simulator_url:
            self.send_to_simulator(bytes(packet))
            return
        # FFFF is the address (broadcast) and 0x12 is channel 18 (default)
        prefix = [0xFF, 0xFF, 0x12]
        to_send = " ".join("%02x" % b for b in itertools.chain(prefix, packet))
        self._write_crlf(to_send)

    def write_bytes(self, packet: bytes) -> None:
        self.write_packet([int(b) for b in packet])
    
    def read_packet(self) -> Optional[List[int]]:
        """
        Read packet in packet mode.
        
        Returns:
            List of bytes received (without address and channel prefix)
        """
        if self.simulator_url:
            data = self.read_from_simulator()
            if not data:
                return None
            return [int(b) for b in data]
        
        hex_bytes = self._read_delim()
        if not hex_bytes:
            return None
        
        r_bytes = [int(b, 16) for b in re.split(r"\s+", hex_bytes) 
                   if re.match(r"[\da-fA-F]{2}$", b)]
        return r_bytes[3:]  # remove first 3 bytes (address and channel)
    
    def read_bytes(self) -> Optional[bytes]:
        int_list = self.read_packet()
        if not int_list:
            return None
        return bytes(int_list)
    
    def configure_packet_mode(self) -> None:
        if self.simulator_url:
            return  # not relevant in simulation
        """Configure modem for packet mode operation."""
        self.at_command("AT+MODE=1")  # enter packet mode
        self.at_command("AT+ADDR=65535")  # listen to all
        self.at_command("AT+LBT=1")  # set ListenBeforeTalk
        self.at_mode_leave()
    

def measure_send_receive_for_testing_class_LoRaModem(modem, do_send: bool = True, packet_size: int = 64) -> None:
    """
    Measure packet send/receive performance.
    
    Args:
        do_send: Whether to send packets
        packet_size: Size of packets to send
        bps: Bits per second rate
    """
    modem.configure_packet_mode()
    
    n_send = 0
    n_prev_recv = 0
    n_recv_err = 0
    n_recv = 0
    packets_sent = 0
    packets_recv = 0
    bps = 300
    bytes_per_second = bps / 10
    
    while True:
        time.sleep(2 * packet_size / bytes_per_second)
        
        # Create packet
        packet = []
        for i in range(packet_size):
            packet.append(n_send)
            n_send = (n_send + 1) & 0xFF
        
        # Send packet if requested
        if do_send:
            modem.write_packet(packet)
            packets_sent += 1
        
        # Read incoming packet
        r_packet = modem.read_packet()
        if not r_packet:
            time.sleep(0.1)
            print("no packet")
            continue
        packets_recv += 1
        # Check packet for errors
        packet_error = 0
        for r in r_packet:
            n_recv += 1
            r_expected = (n_prev_recv + 1) & 0xFF
            if r != r_expected:
                packet_error = 1
            n_prev_recv = r
        
        n_recv_err += packet_error
        print(f"\nRECV errors/pkts sent/pkts recv : {n_recv_err}/{packets_sent}/{packets_recv}    \r", end="")


def main_for_testing_class_LoRaModem(argv):
    """Main function. Used for testing the class LoRaModem"""
    if len(argv) < 2:
        print("ERROR - must provide serial device", file=sys.stderr)
        return 3
    
    device = argv[1]
    
    try:
        with LoRaModem(device) as modem:
            measure_send_receive(modem)
    except KeyboardInterrupt:
        print("\nExiting...")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main_for_testing_class_LoRaModem(sys.argv))